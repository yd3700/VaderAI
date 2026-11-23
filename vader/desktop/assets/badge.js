// 녹화 배지의 [끝내기] — 엄격한 CSP(인라인 실행 금지)라 파일로 둔다.
document.getElementById('end').addEventListener('click', e => { e.currentTarget.disabled = true; window.pywebview.api.call('record_end'); });
