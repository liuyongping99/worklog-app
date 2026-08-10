/* global cv */
window.mobileBlurCheck = async function (file) {
  // 兜底：OpenCV.js 未就绪或失败 → 放行
  if (typeof cv === "undefined" || !cv || !cv.Mat) {
    return { ok: true, variance: -1, dataURL: null, skipped: true };
  }
  try {
    const dataURL = await new Promise((res, rej) => {
      const r = new FileReader();
      r.onload = () => res(r.result);
      r.onerror = rej;
      r.readAsDataURL(file);
    });
    const img = new Image();
    img.src = dataURL;
    await new Promise((res, rej) => { img.onload = res; img.onerror = rej; });
    const canvas = document.createElement("canvas");
    canvas.width = img.naturalWidth;
    canvas.height = img.naturalHeight;
    const ctx = canvas.getContext("2d");
    ctx.drawImage(img, 0, 0);
    const src = cv.imread(canvas);
    const gray = new cv.Mat();
    cv.cvtColor(src, gray, cv.COLOR_RGBA2GRAY);
    const lap = new cv.Mat();
    cv.Laplacian(gray, lap, cv.CV_64F);
    const variance = cv.meanStdDev(lap).stddev[0] ** 2;
    src.delete(); gray.delete(); lap.delete();
    return { ok: variance >= 80, variance, dataURL, skipped: false };
  } catch (e) {
    return { ok: true, variance: -1, dataURL: null, skipped: true, error: String(e) };
  }
};
