"""
图像处理模块：封装 FFT 变换、水印嵌入、逆 FFT 等逻辑。

该模块独立于 HTTP 服务，只负责图像运算，不处理任何网络请求。
"""

import base64
import io
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Dict, Optional, Tuple

import numpy as np
from PIL import Image


@contextmanager
def _timed(label: str):
    """简单的耗时上下文管理器，用于打印 CPU 密集型计算耗时。"""
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed_ms = (time.perf_counter() - start) * 1000
        print(f"[TIMING] {label}: {elapsed_ms:.2f} ms")


# ==================== 可配置常量 ====================
DEFAULT_SCALE_FRONTEND = 50  # 前端水印比例滑块默认值（0~100）
# 0%  表示不显示水印
# 50% 表示 B 的长边 = A 短边的 1/4（默认，覆盖频域 1/8）
# 100% 表示 B 的长边 = A 短边的 1/2

DEFAULT_POWER_FRONTEND = 5   # 前端强度滑块默认值（1~10）
# 1  表示最小强度
# 10 表示非黑区域幅度乘以 10

DEFAULT_FREQ_FRONTEND = 10   # 前端水印频率滑块默认值（0~10）
# 10 表示水印位于频谱四角（高频边缘）
# 0  表示水印位于频谱中心（低频）

# 二值化水印后，非黑区域的掩膜值。
# 实际作用时会再乘以 power，所以这里固定为 1.0。
WATERMARK_MASK_VALUE = 1.0

# 高频区域（频谱角落）原始幅度通常接近 0，纯乘法会导致水印不可见。
# 因此引入一个 floor，保证非黑区域至少有 WATERMARK_AMPLITUDE_FLOOR 的幅度。
WATERMARK_AMPLITUDE_FLOOR = 10.0
# ====================================================


def _frontend_scale_to_ratio(scale_frontend: float) -> float:
    """
    将前端 0~100 的水印比例滑块值映射到实际比例。

    映射规则：
        0%  -> 0
        50% -> 1/4（默认，B 长边 = A 短边的 1/4）
        100% -> 1/2（B 长边 = A 短边的 1/2）
    """
    scale_frontend = float(scale_frontend)
    scale_frontend = max(0.0, min(100.0, scale_frontend))
    return scale_frontend / 200.0


def _pil_to_bytes(img: Image.Image, compress_level: int = 6) -> bytes:
    """将 PIL 图像编码为 PNG 字节流。"""
    buffer = io.BytesIO()
    # 预览接口追求速度，sign 下载接口可以保留默认压缩
    img.save(buffer, format="PNG", compress_level=compress_level, optimize=False)
    return buffer.getvalue()


def _array_to_png_bytes(arr: np.ndarray, compress_level: int = 6) -> bytes:
    """将 numpy 数组（H, W）或（H, W, C）编码为 PNG 字节流。"""
    if arr.ndim == 2:
        img = Image.fromarray(arr, mode="L")
    elif arr.ndim == 3 and arr.shape[2] == 3:
        img = Image.fromarray(arr, mode="RGB")
    else:
        raise ValueError(f"Unsupported array shape: {arr.shape}")
    return _pil_to_bytes(img, compress_level=compress_level)


def _to_data_url(png_bytes: bytes) -> str:
    """将 PNG 字节流转为前端可直接展示的数据 URL。"""
    b64 = base64.b64encode(png_bytes).decode("ascii")
    return f"data:image/png;base64,{b64}"


def _normalize_for_display(arr: np.ndarray) -> np.ndarray:
    """
    将任意范围的浮点数组归一化到 0~255 的 uint8，用于展示。
    """
    arr = arr.astype(np.float64)
    amin, amax = arr.min(), arr.max()
    return _normalize_with_range(arr, amin, amax)


def _normalize_with_range(arr: np.ndarray, amin: float, amax: float) -> np.ndarray:
    """
    使用指定的 [amin, amax] 范围将数组归一化到 0~255。

    超出范围的部分会被裁剪到 0 或 255。这个函数用于让原始幅度谱和
    水印叠加后的幅度谱使用同一套亮度尺度，从而保证水印区域的变化
    在视觉上可见。
    """
    arr = arr.astype(np.float64)
    if amax > amin:
        arr = (arr - amin) / (amax - amin)
    else:
        arr = np.zeros_like(arr)
    arr = np.clip(arr, 0.0, 1.0)
    return (arr * 255.0).astype(np.uint8)


def _colorize_channel(gray: np.ndarray, color: str) -> np.ndarray:
    """
    将单通道灰度图着色为指定通道颜色（r/g/b）。

    返回 H x W x 3 的 RGB 图像，只有对应通道有值，其余两通道为 0。
    这样 R/G/B 三个通道在 3×3 表格中一目了然，不会混淆。
    """
    if gray.ndim != 2:
        raise ValueError("gray must be a 2D array")
    h, w = gray.shape
    colored = np.zeros((h, w, 3), dtype=np.uint8)
    channel_index = {"r": 0, "g": 1, "b": 2}[color]
    colored[:, :, channel_index] = gray
    return colored


def _fft_channel(channel: np.ndarray) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    对单通道图像做 FFT。

    返回：
        fshift: fftshift 后的复数频谱
        amplitude: 幅度谱（已 shift，中心为 0）
        phase: 相位谱（已 shift）
    """
    with _timed("fft_channel"):
        f = np.fft.fft2(channel)
        fshift = np.fft.fftshift(f)
        amplitude = np.abs(fshift)
        phase = np.angle(fshift)
    return fshift, amplitude, phase


def _binarize_watermark(watermark_gray: np.ndarray) -> np.ndarray:
    """
    将灰度水印二值化为纯掩膜。

    规则：
        - 黑色区域表示“不修改”，非黑区域表示签名内容
        - 只区分“黑/非黑”，不关心颜色深浅
        - 非黑区域标记为 WATERMARK_MASK_VALUE，黑色区域标记为 0
    """
    wm = watermark_gray.astype(np.float64)
    # 0 为纯黑，>0 为非黑；为鲁棒性稍微放宽，阈值取 0
    mask = (wm > 0).astype(np.float64) * WATERMARK_MASK_VALUE
    return mask


def _build_watermark_mask(
    watermark_binary: np.ndarray,
    target_shape: Tuple[int, int],
    freq_frontend: float = DEFAULT_FREQ_FRONTEND,
) -> np.ndarray:
    with _timed("build_watermark_mask"):
        return _build_watermark_mask_impl(watermark_binary, target_shape, freq_frontend)


def _build_watermark_mask_impl(
    watermark_binary: np.ndarray,
    target_shape: Tuple[int, int],
    freq_frontend: float = DEFAULT_FREQ_FRONTEND,
) -> np.ndarray:
    """
    根据二值化水印构造与目标图像同大小的能量掩膜。

    规则：
        - 水印复制为四份，分别放在频谱的四个角（沿中心向外方向）
        - freq_frontend = 10 时，四份水印贴到频谱四角（高频边缘）
        - freq_frontend = 0  时，四份水印中心与频谱中心重合（低频）
        - 四份按顺时针依次旋转 90°：
            左上原图、右上顺时针 90°、右下 180°、左下逆时针 90°
        - 同时保持 180° 中心对称，以满足实信号 DFT 的共轭对称性
    """
    h, w = target_shape

    # freq_frontend 范围 0~10
    freq = float(freq_frontend)
    freq = max(0.0, min(10.0, freq))
    ratio = freq / 10.0

    # 频谱中心（fftshift 后的几何中心）
    cy = h / 2.0
    cx = w / 2.0

    # 四份图案：顺时针依次为 0°、90°、180°、270°
    p0 = watermark_binary
    p90 = np.rot90(watermark_binary, -1)   # 顺时针 90°
    p180 = watermark_binary[::-1, ::-1]    # 180°
    p270 = np.rot90(watermark_binary, 1)   # 逆时针 90°

    # (图案, 垂直方向系数, 水平方向系数)
    patterns = [
        (p0,   -1, -1),  # 左上
        (p90,  -1,  1),  # 右上
        (p180,  1,  1),  # 右下
        (p270,  1, -1),  # 左下
    ]

    mask = np.zeros((h, w), dtype=np.float64)

    for pattern, dy, dx in patterns:
        ph, pw = pattern.shape

        # 最大偏移：使该份水印中心正好贴到对应边缘
        max_oy = cy - ph / 2.0
        max_ox = cx - pw / 2.0

        oy = max_oy * ratio
        ox = max_ox * ratio

        py = cy + dy * oy
        px = cx + dx * ox

        y1 = int(round(py - ph / 2.0))
        x1 = int(round(px - pw / 2.0))
        y2 = y1 + ph
        x2 = x1 + pw

        # 裁剪到图像边界内
        y1c = max(0, min(h, y1))
        x1c = max(0, min(w, x1))
        y2c = max(0, min(h, y2))
        x2c = max(0, min(w, x2))

        # 对应裁剪 pattern
        sy1 = y1c - y1
        sx1 = x1c - x1
        sy2 = sy1 + (y2c - y1c)
        sx2 = sx1 + (x2c - x1c)

        if y2c > y1c and x2c > x1c:
            mask[y1c:y2c, x1c:x2c] = np.maximum(
                mask[y1c:y2c, x1c:x2c], pattern[sy1:sy2, sx1:sx2]
            )

    return mask


class FFTWatermarkProcessor:
    """
    FFT 水印处理器。

    对外提供三个主要能力：
        1. analyze()       : 分析原图，返回 3×3 所需的 9 张图
        2. preview_watermark(): 预览水印叠加到幅度谱后的效果
        3. sign()          : 执行逆 FFT，生成带水印的图像
    """

    def __init__(self, image_bytes: bytes):
        """
        加载原图 A。

        Args:
            image_bytes: 原图 jpg/png 字节流
        """
        self.image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        self.width, self.height = self.image.size
        self.arr = np.array(self.image)  # H x W x 3, uint8

        # 预计算三通道 FFT 以及用于显示的对数幅度谱范围
        self._fshift_channels = []
        self._amplitude_channels = []
        self._phase_channels = []
        self._amp_log_channels = []
        self._amp_log_ranges = []  # 每个通道 (amin, amax)

        with _timed("processor_init_fft_all_channels"):
            for c in range(3):
                channel = self.arr[:, :, c].astype(np.float64)
                fshift, amplitude, phase = _fft_channel(channel)
                self._fshift_channels.append(fshift)
                self._amplitude_channels.append(amplitude)
                self._phase_channels.append(phase)

                amp_log = np.log1p(amplitude)
                self._amp_log_channels.append(amp_log)
                self._amp_log_ranges.append((amp_log.min(), amp_log.max()))

        # 水印缓存：B 上传后缓存 bytes，并按 scale 缓存二值化结果
        self._watermark_bytes: Optional[bytes] = None
        self._watermark_cache: Dict[float, np.ndarray] = {}

        # mask 缓存：按 (scale, freq) 缓存构建好的水印掩膜
        self._mask_cache: Dict[Tuple[float, float], np.ndarray] = {}

        # 线程池：用于并行处理 R/G/B 三个通道
        self._executor = ThreadPoolExecutor(max_workers=3)

    def set_watermark(self, watermark_bytes: bytes):
        """
        设置水印图 B。

        后续 preview_watermark / sign 可以不传 watermark_bytes，
        直接使用缓存的 B 进行计算。
        """
        self._watermark_bytes = watermark_bytes
        self._watermark_cache.clear()
        self._mask_cache.clear()

    def _get_mask(self, scale_frontend: float, freq_frontend: float) -> np.ndarray:
        """获取/缓存指定 (scale, freq) 下的水印掩膜。"""
        key = (float(scale_frontend), float(freq_frontend))
        if key not in self._mask_cache:
            wm_binary = self._get_watermark_binary(scale_frontend)
            self._mask_cache[key] = _build_watermark_mask(
                wm_binary, (self.height, self.width), freq_frontend
            )
        return self._mask_cache[key]

    def _preview_channel(self, c: int, mask: np.ndarray, power: float) -> np.ndarray:
        """计算单个通道叠加水印后的幅度谱显示数组。"""
        with _timed(f"preview_channel_{['r','g','b'][c]}"):
            amplitude_orig = self._amplitude_channels[c]
            amplitude_masked = np.maximum(amplitude_orig, WATERMARK_AMPLITUDE_FLOOR) * power
            amplitude_new = amplitude_orig * (1.0 - mask) + amplitude_masked * mask

            amp_log = np.log1p(amplitude_new)
            amin, amax = self._amp_log_ranges[c]
            return _normalize_with_range(amp_log, amin, amax)

    def _sign_channel(self, c: int, mask: np.ndarray, power: float) -> np.ndarray:
        """计算单个通道签名后的空域图像。"""
        with _timed(f"sign_channel_{['r','g','b'][c]}"):
            amplitude_orig = self._amplitude_channels[c]
            amplitude_masked = np.maximum(amplitude_orig, WATERMARK_AMPLITUDE_FLOOR) * power
            amplitude_new = amplitude_orig * (1.0 - mask) + amplitude_masked * mask

            # 用新幅度谱和原相位谱重建复数频谱
            fshift_new = amplitude_new * np.exp(1j * self._phase_channels[c])

            # 逆变换回空域
            f_new = np.fft.ifftshift(fshift_new)
            channel_new = np.real(np.fft.ifft2(f_new))

            # 裁剪并转为 uint8
            return np.clip(channel_new, 0, 255).astype(np.uint8)

    def _get_watermark_binary(self, scale_frontend: float) -> np.ndarray:
        """获取指定 scale 下的二值化水印，内部做缓存。"""
        if self._watermark_bytes is None:
            raise ValueError("水印未设置，请先调用 set_watermark 或直接传入 watermark_bytes")

        scale_frontend = float(scale_frontend)
        if scale_frontend not in self._watermark_cache:
            self._watermark_cache[scale_frontend] = self._prepare_watermark(
                self._watermark_bytes, scale_frontend
            )
        return self._watermark_cache[scale_frontend]

    def analyze(self) -> Dict[str, str]:
        """
        分析原图，返回 3×3 展示所需的 9 张图（数据 URL 形式）。

        返回字典结构：
            {
                "original": <原图>,
                "spatial": {"r": ..., "g": ..., "b": ...},
                "amplitude": {"r": ..., "g": ..., "b": ...},
                "phase": {"r": ..., "g": ..., "b": ...}
            }
        """
        result = {
            "original": _to_data_url(_array_to_png_bytes(self.arr)),
            "spatial": {},
            "amplitude": {},
            "phase": {},
        }

        colors = ["r", "g", "b"]
        with _timed("analyze_encode_all_channels"):
            for c, color in enumerate(colors):
                # 空域：单通道按 R/G/B 着色显示
                spatial_colored = _colorize_channel(self.arr[:, :, c], color)
                spatial_png = _array_to_png_bytes(spatial_colored)
                result["spatial"][color] = _to_data_url(spatial_png)

                # 频域幅度谱：对数变换后，用原始幅度谱的 min/max 归一化显示
                amp_log = self._amp_log_channels[c]
                amin, amax = self._amp_log_ranges[c]
                amp_png = _array_to_png_bytes(_normalize_with_range(amp_log, amin, amax))
                result["amplitude"][color] = _to_data_url(amp_png)

                # 相位谱：归一化到 0~255 显示（相位谱本来就是灰度图）
                phase_png = _array_to_png_bytes(_normalize_for_display(self._phase_channels[c]))
                result["phase"][color] = _to_data_url(phase_png)

        return result

    def _prepare_watermark(self, watermark_bytes: bytes, scale_frontend: float) -> np.ndarray:
        """
        加载水印图 B，转灰度图、二值化，并按比例 resize。

        缩放规则：
            - scale_frontend = 0  时返回 1×1 的零矩阵（不显示水印）
            - scale_frontend = 50 时，B 的长边 = A 短边的 1/4（默认，覆盖频域 1/8）
            - scale_frontend = 100 时，B 的长边 = A 短边的 1/2
            - 始终保持 B 的原始长宽比不变
        """
        with _timed("prepare_watermark"):
            wm = Image.open(io.BytesIO(watermark_bytes)).convert("L")

            scale_ratio = _frontend_scale_to_ratio(scale_frontend)
            if scale_ratio <= 0:
                # 0% 时不显示水印，返回 1×1 零矩阵
                return np.zeros((1, 1), dtype=np.float64)

            # A 的短边
            a_short = min(self.width, self.height)
            target_long = int(round(a_short * scale_ratio))
            target_long = max(1, target_long)

            # B 的原始尺寸和长边
            b_w, b_h = wm.size
            b_long = max(b_w, b_h)
            if b_long == 0:
                b_long = 1

            # 按长边比例缩放，保持宽高比
            scale = target_long / b_long
            new_w = max(1, int(round(b_w * scale)))
            new_h = max(1, int(round(b_h * scale)))

            # 水印图本质是二值图，resize 必须用最近邻插值，
            # 避免 LANCZOS 在边缘产生灰度过渡像素或文字内部空洞。
            wm = wm.resize((new_w, new_h), Image.Resampling.NEAREST)
            wm_gray = np.array(wm, dtype=np.float64)

            # 二值化：只区分黑/非黑
            return _binarize_watermark(wm_gray)

    def _preview_watermark_arrays(
        self,
        watermark_bytes: Optional[bytes] = None,
        scale_frontend: float = DEFAULT_SCALE_FRONTEND,
        power_frontend: float = DEFAULT_POWER_FRONTEND,
        freq_frontend: float = DEFAULT_FREQ_FRONTEND,
    ) -> Dict[str, np.ndarray]:
        """
        内部方法：计算叠加后的幅度谱数组。

        返回：
            {"r": array, "g": array, "b": array}
        """
        power = float(power_frontend)
        power = max(1.0, min(10.0, power))

        if watermark_bytes is not None:
            # 临时模式：不修改缓存，直接构建 mask
            wm_binary = self._prepare_watermark(watermark_bytes, scale_frontend)
            mask = _build_watermark_mask(wm_binary, (self.height, self.width), freq_frontend)
        else:
            mask = self._get_mask(scale_frontend, freq_frontend)

        # 并行计算 R/G/B 三个通道
        with _timed("preview_watermark_parallel_rgb"):
            futures = [
                self._executor.submit(self._preview_channel, c, mask, power)
                for c in range(3)
            ]
            results = [f.result() for f in futures]

        return {"r": results[0], "g": results[1], "b": results[2]}

    def preview_watermark(
        self,
        watermark_bytes: Optional[bytes] = None,
        scale_frontend: float = DEFAULT_SCALE_FRONTEND,
        power_frontend: float = DEFAULT_POWER_FRONTEND,
        freq_frontend: float = DEFAULT_FREQ_FRONTEND,
    ) -> Dict[str, str]:
        """
        将二值化水印叠加到幅度谱上，返回 data URL 形式的结果。

        Args:
            watermark_bytes: 水印图 B 的字节流；为 None 时使用 set_watermark 缓存的 B
            scale_frontend: 前端水印比例滑块值 0~100
            power_frontend: 前端强度滑块值 1~10

        返回：
            {"amplitude": {"r": data_url, "g": data_url, "b": data_url}}
        """
        arrays = self._preview_watermark_arrays(
            watermark_bytes=watermark_bytes,
            scale_frontend=scale_frontend,
            power_frontend=power_frontend,
            freq_frontend=freq_frontend,
        )
        return {
            "amplitude": {
                color: _to_data_url(_array_to_png_bytes(arr))
                for color, arr in arrays.items()
            }
        }

    def preview_watermark_bytes(
        self,
        watermark_bytes: Optional[bytes] = None,
        scale_frontend: float = DEFAULT_SCALE_FRONTEND,
        power_frontend: float = DEFAULT_POWER_FRONTEND,
        freq_frontend: float = DEFAULT_FREQ_FRONTEND,
    ) -> Dict[str, bytes]:
        """
        将二值化水印叠加到幅度谱上，返回 PNG bytes 形式的结果。

        Args:
            watermark_bytes: 水印图 B 的字节流；为 None 时使用 set_watermark 缓存的 B
            scale_frontend: 前端水印比例滑块值 0~100
            power_frontend: 前端强度滑块值 1~10

        返回：
            {"r": png_bytes, "g": png_bytes, "b": png_bytes}
        """
        arrays = self._preview_watermark_arrays(
            watermark_bytes=watermark_bytes,
            scale_frontend=scale_frontend,
            power_frontend=power_frontend,
            freq_frontend=freq_frontend,
        )
        # 预览接口优先速度，使用无压缩 PNG
        return {color: _array_to_png_bytes(arr, compress_level=0) for color, arr in arrays.items()}

    def sign(
        self,
        watermark_bytes: Optional[bytes] = None,
        scale_frontend: float = DEFAULT_SCALE_FRONTEND,
        power_frontend: float = DEFAULT_POWER_FRONTEND,
        freq_frontend: float = DEFAULT_FREQ_FRONTEND,
    ) -> bytes:
        """
        执行水印签名：修改幅度谱 + 原相位谱 -> 逆 FFT -> 生成带水印图像。

        Args:
            watermark_bytes: 水印图 B 的字节流；为 None 时使用 set_watermark 缓存的 B
            scale_frontend: 前端水印比例滑块值 0~100
            power_frontend: 前端强度滑块值 1~10

        返回：
            带水印图像的 PNG 字节流
        """
        power = float(power_frontend)
        power = max(1.0, min(10.0, power))

        if watermark_bytes is not None:
            # 临时模式：不修改缓存，直接构建 mask
            wm_binary = self._prepare_watermark(watermark_bytes, scale_frontend)
            mask = _build_watermark_mask(wm_binary, (self.height, self.width), freq_frontend)
        else:
            mask = self._get_mask(scale_frontend, freq_frontend)

        # sign 涉及 IFFT，测试发现外部并行反而更慢（线程调度开销 > 收益）
        # 因此保持串行，依赖 numpy 内部优化
        with _timed("sign_all_channels"):
            signed_channels = [self._sign_channel(c, mask, power) for c in range(3)]

        signed_img = np.stack(signed_channels, axis=2)  # H x W x 3
        # 本地服务优先响应速度，签名图也使用无压缩 PNG
        return _array_to_png_bytes(signed_img, compress_level=0)

    def close(self):
        """释放线程池资源。"""
        self._executor.shutdown(wait=False)
