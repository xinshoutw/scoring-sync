(function () {
    const inputs = document.querySelectorAll('input[type="number"][data-score]');

    function clampInput(el) {
        const min = Number(el.min);
        const max = Number(el.max);
        let v = parseInt(el.value, 10);
        if (Number.isNaN(v)) v = min;
        if (v < min) v = min;
        if (v > max) v = max;
        el.value = v;
    }

    function updateTotal() {
        let sum = 0;
        inputs.forEach((i) => { sum += Number(i.value) || 0; });
        const totalEl = document.getElementById('total');
        if (totalEl) {
            totalEl.textContent = String(sum);
            totalEl.classList.toggle('warning', sum < Number(totalEl.dataset.max || 100));
        }
    }

    inputs.forEach((el) => {
        // 滾輪鎖：focus 時禁止 wheel 改值
        el.addEventListener('wheel', (e) => {
            if (document.activeElement === el) {
                e.preventDefault();
            }
        }, { passive: false });

        // 失焦 + 輸入即時 clamp
        el.addEventListener('input', () => { clampInput(el); updateTotal(); });
        el.addEventListener('blur', () => { clampInput(el); updateTotal(); });

        // 按 Enter 送出
        el.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                e.preventDefault();
                const form = el.closest('form');
                if (form) form.requestSubmit();
            }
        });
    });

    // 首次進頁面 focus 第一欄
    if (inputs.length > 0) {
        inputs[0].focus();
        inputs[0].select();
    }

    updateTotal();
})();
