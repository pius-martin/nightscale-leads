/* Shared progressive enhancements: confirm dialogs, loading states,
   flash dismissal, local timestamps. Everything degrades gracefully —
   without JS, forms still submit natively. */
(function () {
    'use strict';

    // ---- Styled confirm dialog (form[data-confirm] / button[data-confirm])
    var dialog = document.getElementById('confirm-dialog');

    function openConfirm(message, onConfirm) {
        if (!dialog || typeof HTMLDialogElement !== 'function') {
            if (window.confirm(message)) onConfirm();
            return;
        }
        dialog.querySelector('#confirm-message').textContent = message;
        var okBtn = dialog.querySelector('#confirm-ok');
        var cancelBtn = dialog.querySelector('#confirm-cancel');
        function cleanup() {
            okBtn.removeEventListener('click', ok);
            cancelBtn.removeEventListener('click', cancel);
            dialog.close();
        }
        function ok() { cleanup(); onConfirm(); }
        function cancel() { cleanup(); }
        okBtn.addEventListener('click', ok);
        cancelBtn.addEventListener('click', cancel);
        dialog.showModal();
    }

    document.addEventListener('submit', function (e) {
        var form = e.target;
        if (!(form instanceof HTMLFormElement)) return;

        var submitter = e.submitter || null;
        var message = (submitter && submitter.dataset.confirm) || form.dataset.confirm;

        if (message && !form.dataset.confirmed) {
            e.preventDefault();
            openConfirm(message, function () {
                form.dataset.confirmed = '1';
                if (form.requestSubmit) {
                    form.requestSubmit(submitter || undefined);
                } else {
                    if (submitter && submitter.name) {
                        var h = document.createElement('input');
                        h.type = 'hidden';
                        h.name = submitter.name;
                        h.value = submitter.value;
                        form.appendChild(h);
                    }
                    form.submit();
                }
            });
            return;
        }
        delete form.dataset.confirmed;

        // ---- Loading state (form[data-loading])
        var loadingText = form.dataset.loading;
        if (loadingText) {
            var btn = submitter || form.querySelector('button[type="submit"], button:not([type])');
            if (btn) {
                btn.classList.add('btn-loading');
                btn.textContent = loadingText;
                // Disable only after the submit has been dispatched so the
                // button's name/value still makes it into the request.
                requestAnimationFrame(function () { btn.disabled = true; });
            }
        }
    });

    // ---- Flash messages: dismiss button + auto-fade for successes
    document.querySelectorAll('.flash').forEach(function (el) {
        var close = document.createElement('button');
        close.type = 'button';
        close.className = 'flash-close';
        close.setAttribute('aria-label', 'Dismiss');
        close.textContent = '×';
        close.addEventListener('click', function () { el.remove(); });
        el.appendChild(close);
        if (el.classList.contains('success')) {
            setTimeout(function () {
                el.classList.add('fade-out');
                setTimeout(function () { el.remove(); }, 400);
            }, 6000);
        }
    });

    // ---- Local timestamps ([data-ts] holds an ISO datetime)
    document.querySelectorAll('[data-ts]').forEach(function (el) {
        var iso = el.dataset.ts;
        if (!iso) return;
        var d = new Date(iso);
        if (isNaN(d.getTime())) return;
        el.title = iso;
        el.textContent = d.toLocaleString(undefined, {
            year: 'numeric', month: '2-digit', day: '2-digit',
            hour: '2-digit', minute: '2-digit',
        });
    });
})();
