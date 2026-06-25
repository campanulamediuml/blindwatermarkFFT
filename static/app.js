/**
 * FFT 水印工具前端逻辑（高性能会话版）。
 *
 * 优化点：
 *   1. 原图 A 和水印 B 上传一次后由服务端缓存（session）。
 *   2. 调参只发送 session_id 和参数，不再重复上传图片。
 *   3. 预览接口返回二进制 PNG / multipart，避免 base64 JSON 的巨大开销。
 *
 * 功能：
 *   1. 拖拽/点击上传原图 A 和水印图 B
 *   2. 上传 A 后显示原图和 3×3 频域分析表
 *   3. 上传 B 或调整参数后实时预览幅度谱叠加效果
 *   4. 点击“签名”生成并下载带水印图像
 */

(function () {
    // ==================== 状态 ====================
    let fileA = null;
    let fileB = null;
    let sessionId = null;   // 服务端会话 ID
    let currentScale = 50;  // 水印比例 0~100
    let currentPower = 5;   // 强度 1~10
    let currentFreq = 10;   // 水印频率 0~10，10 为边缘，0 为中心
    let analyzeResult = null; // 缓存 analyze 结果

    // ==================== DOM 元素 ====================
    const dropZoneA = document.getElementById('drop-zone-a');
    const dropZoneB = document.getElementById('drop-zone-b');
    const fileInputA = document.getElementById('file-a');
    const fileInputB = document.getElementById('file-b');
    const fileNameA = document.getElementById('file-name-a');
    const fileNameB = document.getElementById('file-name-b');

    const scaleSlider = document.getElementById('scale-slider');
    const scaleValue = document.getElementById('scale-value');
    const powerSlider = document.getElementById('power-slider');
    const powerValue = document.getElementById('power-value');
    const freqSlider = document.getElementById('freq-slider');
    const freqValue = document.getElementById('freq-value');
    const signBtn = document.getElementById('sign-btn');
    const downloadLink = document.getElementById('download-link');

    const originalPreview = document.getElementById('original-preview');
    const originalPlaceholder = document.getElementById('original-placeholder');
    const resultPreview = document.getElementById('result-preview');
    const resultPlaceholder = document.getElementById('result-placeholder');
    const previewSignImage = document.getElementById('preview-sign-image');
    const previewSignPlaceholder = document.getElementById('preview-sign-placeholder');
    const previewSignLoading = document.getElementById('preview-sign-loading');
    const gridLoading = document.getElementById('grid-loading');
    const gridTable = document.querySelector('.spectrum-grid');

    // 图片放大模态框
    const imageModal = document.getElementById('image-modal');
    const modalImage = document.getElementById('modal-image');
    const modalClose = document.querySelector('.modal-close');

    // 3×3 图像元素
    const imageIds = {
        spatial: ['spatial-r', 'spatial-g', 'spatial-b'],
        amplitude: ['amplitude-r', 'amplitude-g', 'amplitude-b'],
        phase: ['phase-r', 'phase-g', 'phase-b'],
    };

    // 用于取消未完成的预览请求
    let previewAbortController = null;
    let previewSignAbortController = null;

    // ==================== 工具函数 ====================

    /**
     * 显示/隐藏加载提示。
     */
    function setLoading(element, isLoading) {
        if (isLoading) {
            element.style.opacity = '0.5';
        } else {
            element.style.opacity = '1';
        }
    }

    /**
     * 通用的文件 POST 请求。
     */
    function postFile(url, formData, signal, asJson = true) {
        const options = {
            method: 'POST',
            body: formData,
        };
        if (signal) {
            options.signal = signal;
        }
        return fetch(url, options).then(response => {
            if (!response.ok) {
                return response.text().then(text => {
                    throw new Error(`请求失败：${response.status} ${text}`);
                });
            }
            if (asJson) {
                return response.json();
            }
            return response;
        });
    }

    /**
     * 设置图片元素的 src 并显示。
     */
    function showImage(element, src) {
        element.src = src;
        element.style.display = 'block';
    }

    /**
     * 隐藏所有 3×3 网格中的图片。
     */
    function hideGridImages() {
        Object.values(imageIds).flat().forEach(id => {
            const el = document.getElementById(id);
            el.style.display = 'none';
            el.src = '';
        });
    }

    /**
     * 显示 3×3 频域分析表加载动画，并隐藏表格本身。
     */
    function showGridLoading() {
        gridLoading.style.display = 'block';
        gridTable.style.display = 'none';
    }

    /**
     * 隐藏 3×3 频域分析表加载动画，并显示表格本身。
     */
    function hideGridLoading() {
        gridLoading.style.display = 'none';
        gridTable.style.display = '';
    }

    /**
     * 填充 3×3 网格。
     */
    function fillGrid(result) {
        const colors = ['r', 'g', 'b'];
        colors.forEach((color, idx) => {
            showImage(document.getElementById(imageIds.spatial[idx]), result.spatial[color]);
            showImage(document.getElementById(imageIds.amplitude[idx]), result.amplitude[color]);
            showImage(document.getElementById(imageIds.phase[idx]), result.phase[color]);
        });
    }

    /**
     * 更新幅度谱单元格（用于水印预览）。
     */
    function updateAmplitudeCells(urls) {
        const colors = ['r', 'g', 'b'];
        colors.forEach((color, idx) => {
            showImage(document.getElementById(imageIds.amplitude[idx]), urls[color]);
        });
    }

    /**
     * 恢复幅度谱为原始状态（没有水印 B 时）。
     */
    function restoreOriginalAmplitude() {
        if (!analyzeResult) return;
        const colors = ['r', 'g', 'b'];
        colors.forEach((color, idx) => {
            showImage(document.getElementById(imageIds.amplitude[idx]), analyzeResult.amplitude[color]);
        });
    }

    /**
     * 更新按钮状态。
     */
    function updateControls() {
        signBtn.disabled = !(fileA && fileB);
    }

    /**
     * 清空签名结果区域（当 A 或 B 发生变化时调用）。
     */
    function clearResult() {
        resultPreview.src = '';
        resultPreview.style.display = 'none';
        resultPlaceholder.style.display = 'block';
        downloadLink.style.display = 'none';
        downloadLink.href = '#';
    }

    /**
     * 清空签名预览区域。
     */
    function clearPreviewSign() {
        previewSignImage.src = '';
        previewSignImage.style.display = 'none';
        previewSignLoading.style.display = 'none';
        previewSignPlaceholder.style.display = 'block';
    }

    /**
     * 更新签名预览区域。
     */
    function updatePreviewSign(src) {
        previewSignPlaceholder.style.display = 'none';
        previewSignLoading.style.display = 'none';
        showImage(previewSignImage, src);
    }

    /**
     * 处理拖拽进入/离开的视觉反馈，并直接调用上传处理函数。
     */
    function setupDragZone(zone, handler) {
        zone.addEventListener('dragover', (e) => {
            e.preventDefault();
            zone.classList.add('dragover');
        });
        zone.addEventListener('dragleave', () => {
            zone.classList.remove('dragover');
        });
        zone.addEventListener('drop', (e) => {
            e.preventDefault();
            zone.classList.remove('dragover');
            const files = e.dataTransfer.files;
            if (files.length > 0) {
                console.log(`[DragDrop] dropped file: ${files[0].name}`);
                handler(files[0]);
            }
        });
    }

    // ==================== 业务逻辑 ====================

    /**
     * 原图 A 上传并分析。
     */
    function handleImageA(file) {
        console.log(`[handleImageA] file: ${file.name}, size: ${file.size}`);
        fileA = file;
        fileNameA.textContent = file.name;

        // 上传新的 A 时，清空之前的会话、签名结果、签名预览和频域叠加效果
        sessionId = null;
        fileB = null;
        fileNameB.textContent = '未选择文件';
        clearResult();
        clearPreviewSign();

        // 原图立刻本地展示，无需等待服务端返回
        const localObjectUrl = URL.createObjectURL(file);
        originalPlaceholder.style.display = 'none';
        showImage(originalPreview, localObjectUrl);

        // 签名预览区域也先展示原图，让用户有即时反馈
        updatePreviewSign(localObjectUrl);

        // 3×3 频域分析表进入加载状态
        hideGridImages();
        showGridLoading();

        const formData = new FormData();
        formData.append('image', file);

        postFile('/api/session/upload', formData, null, true)
            .then(result => {
                console.log('[session/upload] success');
                if (result.error) {
                    throw new Error(result.error);
                }
                sessionId = result.session_id;
                analyzeResult = result.analyze;

                // 服务端分析完成后，替换为更准确的分析结果原图（尺寸等一致）
                showImage(originalPreview, result.analyze.original);
                updatePreviewSign(result.analyze.original);

                // 填充 3×3 表
                hideGridLoading();
                fillGrid(result.analyze);

                updateControls();
            })
            .catch(err => {
                hideGridLoading();
                alert('原图分析失败：' + err.message);
                console.error(err);
            });
    }

    /**
     * 水印图 B 上传。
     */
    function handleImageB(file) {
        console.log(`[handleImageB] file: ${file.name}, size: ${file.size}`);
        fileB = file;
        fileNameB.textContent = file.name;

        // 上传新的 B 时，清空之前的签名结果和签名预览
        clearResult();
        clearPreviewSign();

        if (!sessionId) {
            console.log('[handleImageB] waiting for image A');
            updateControls();
            return;
        }

        const formData = new FormData();
        formData.append('session_id', sessionId);
        formData.append('watermark', file);

        postFile('/api/session/watermark', formData, null, true)
            .then(result => {
                console.log('[session/watermark] success');
                if (result.error) {
                    throw new Error(result.error);
                }
                previewWatermark();
                previewSignedImage();
                updateControls();
            })
            .catch(err => {
                alert('水印上传失败：' + err.message);
                console.error(err);
            });
    }

    /**
     * 调用 /api/session/preview 预览水印叠加效果。
     */
    function previewWatermark() {
        if (!sessionId || !fileB) return;

        console.log(`[previewWatermark] scale=${currentScale}, power=${currentPower}, freq=${currentFreq}`);

        // 取消上一次的幅度谱预览请求
        if (previewAbortController) {
            previewAbortController.abort();
        }
        previewAbortController = new AbortController();

        // 给幅度谱图片添加加载中视觉反馈
        imageIds.amplitude.forEach(id => {
            const el = document.getElementById(id);
            if (el.style.display !== 'none') {
                setLoading(el, true);
            }
        });

        const formData = new FormData();
        formData.append('session_id', sessionId);
        formData.append('scale', currentScale);
        formData.append('power', currentPower);
        formData.append('freq', currentFreq);

        postFile('/api/session/preview', formData, previewAbortController.signal, false)
            .then(response => response.formData())
            .then(formData => {
                console.log('[previewWatermark] response received, updating amplitude cells');
                const urls = {
                    r: URL.createObjectURL(formData.get('amplitude_r')),
                    g: URL.createObjectURL(formData.get('amplitude_g')),
                    b: URL.createObjectURL(formData.get('amplitude_b')),
                };
                updateAmplitudeCells(urls);
            })
            .catch(err => {
                if (err.name === 'AbortError') {
                    console.log('[previewWatermark] request aborted');
                    return;
                }
                alert('水印预览失败：' + err.message);
                console.error(err);
            })
            .finally(() => {
                imageIds.amplitude.forEach(id => {
                    setLoading(document.getElementById(id), false);
                });
            });

    }

    /**
     * 调用 /api/session/preview_sign 实时预览签名后的图像。
     */
    function previewSignedImage() {
        if (!sessionId) return;

        // 如果没有水印 B，签名预览就是原图本身
        if (!fileB) {
            if (analyzeResult) {
                updatePreviewSign(analyzeResult.original);
            }
            return;
        }

        console.log(`[previewSignedImage] scale=${currentScale}, power=${currentPower}, freq=${currentFreq}`);

        // 取消上一次的签名预览请求
        if (previewSignAbortController) {
            previewSignAbortController.abort();
        }
        previewSignAbortController = new AbortController();

        // 显示“少女祈祷中”加载动画
        previewSignPlaceholder.style.display = 'none';
        previewSignImage.style.display = 'none';
        previewSignLoading.style.display = 'block';

        const formData = new FormData();
        formData.append('session_id', sessionId);
        formData.append('scale', currentScale);
        formData.append('power', currentPower);
        formData.append('freq', currentFreq);

        postFile('/api/session/preview_sign', formData, previewSignAbortController.signal, false)
            .then(response => response.blob())
            .then(blob => {
                console.log('[previewSignedImage] response received');
                updatePreviewSign(URL.createObjectURL(blob));
            })
            .catch(err => {
                if (err.name === 'AbortError') {
                    console.log('[previewSignedImage] request aborted');
                    return;
                }
                // 出错时恢复占位提示
                previewSignLoading.style.display = 'none';
                previewSignPlaceholder.style.display = 'block';
                console.error('签名预览失败：' + err.message);
                console.error(err);
            });
    }

    /**
     * 调用 /api/session/sign 生成带水印图像。
     */
    function signImage() {
        if (!sessionId || !fileB) return;

        signBtn.disabled = true;
        signBtn.textContent = '处理中...';

        const formData = new FormData();
        formData.append('session_id', sessionId);
        formData.append('scale', currentScale);
        formData.append('power', currentPower);
        formData.append('freq', currentFreq);

        postFile('/api/session/sign', formData, null, false)
            .then(response => response.blob())
            .then(blob => {
                const url = URL.createObjectURL(blob);

                // 在“签名结果”区域显示
                resultPlaceholder.style.display = 'none';
                showImage(resultPreview, url);

                // 同时更新“签名预览”区域
                updatePreviewSign(url);

                downloadLink.href = url;
                downloadLink.style.display = 'inline-block';

                signBtn.disabled = false;
                signBtn.textContent = '签名';
            })
            .catch(err => {
                alert('签名失败：' + err.message);
                console.error(err);
                signBtn.disabled = false;
                signBtn.textContent = '签名';
            });
    }

    // ==================== 事件绑定 ====================

    setupDragZone(dropZoneA, handleImageA);
    setupDragZone(dropZoneB, handleImageB);

    fileInputA.addEventListener('change', (e) => {
        if (e.target.files.length > 0) {
            handleImageA(e.target.files[0]);
        }
    });

    fileInputB.addEventListener('change', (e) => {
        if (e.target.files.length > 0) {
            handleImageB(e.target.files[0]);
        }
    });

    // 滑动条拖动时实时预览幅度谱（fft 叠图快），松开鼠标后才预览签名（ifft 较慢）
    function onPreviewControlChanged() {
        if (sessionId && fileB) {
            previewWatermark();
        }
    }

    function onSignControlChanged() {
        if (sessionId && fileB) {
            previewSignedImage();
        }
    }

    scaleSlider.addEventListener('input', (e) => {
        currentScale = parseInt(e.target.value, 10);
        scaleValue.textContent = currentScale;
        onPreviewControlChanged();
    });

    scaleSlider.addEventListener('change', (e) => {
        currentScale = parseInt(e.target.value, 10);
        scaleValue.textContent = currentScale;
        onSignControlChanged();
    });

    powerSlider.addEventListener('input', (e) => {
        currentPower = parseInt(e.target.value, 10);
        powerValue.textContent = currentPower;
        onPreviewControlChanged();
    });

    powerSlider.addEventListener('change', (e) => {
        currentPower = parseInt(e.target.value, 10);
        powerValue.textContent = currentPower;
        onSignControlChanged();
    });

    freqSlider.addEventListener('input', (e) => {
        currentFreq = parseInt(e.target.value, 10);
        freqValue.textContent = currentFreq;
        onPreviewControlChanged();
    });

    freqSlider.addEventListener('change', (e) => {
        currentFreq = parseInt(e.target.value, 10);
        freqValue.textContent = currentFreq;
        onSignControlChanged();
    });

    signBtn.addEventListener('click', signImage);

    // ==================== 图片放大查看 ====================

    /**
     * 打开模态框显示大图。
     */
    function openModal(src) {
        modalImage.src = src;
        imageModal.classList.add('active');
    }

    /**
     * 关闭模态框。
     */
    function closeModal() {
        imageModal.classList.remove('active');
        modalImage.src = '';
    }

    // 为所有可放大的图片绑定点击事件
    function bindZoomEvents() {
        const zoomableImages = [
            originalPreview,
            resultPreview,
            previewSignImage,
            ...Object.values(imageIds).flat().map(id => document.getElementById(id)),
        ];

        zoomableImages.forEach(img => {
            img.addEventListener('click', () => {
                if (img.src && img.style.display !== 'none') {
                    openModal(img.src);
                }
            });
        });
    }

    modalClose.addEventListener('click', closeModal);
    imageModal.addEventListener('click', (e) => {
        if (e.target === imageModal) {
            closeModal();
        }
    });
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && imageModal.classList.contains('active')) {
            closeModal();
        }
    });

    // 初始化
    bindZoomEvents();
    updateControls();
})();
