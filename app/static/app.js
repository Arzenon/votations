/* Progressive enhancement, kept tiny and dependency-free.
 *
 * This exists so the pages need no inline scripts and no inline styles, which is
 * what lets the Content-Security-Policy stay strict (`script-src 'self'`, no
 * 'unsafe-inline'). Without JavaScript the site still works: the bars simply
 * render at their default width, and every percentage is written out in text
 * beside them anyway.
 *
 * Confirmations used to live here too, behind a `data-confirm` attribute. They
 * do not any more: a confirmation that only exists in JavaScript is not a
 * confirmation, and archiving or deleting a votation now goes through a page the
 * server renders. What remains is presentation. */
(function () {
  "use strict";

  /* Results bars carry their width as data-pct rather than style="width:…". */
  function paintBars(root) {
    var bars = (root || document).querySelectorAll(".bar-fill[data-pct]");
    Array.prototype.forEach.call(bars, function (bar) {
      var pct = parseFloat(bar.getAttribute("data-pct"));
      if (!isNaN(pct)) {
        bar.style.width = Math.max(0, Math.min(100, pct)) + "%";
      }
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    paintBars(document);
  });

  /* No fragment swapped in today carries bars — the ballot and the draft banner
   * do not. Keeping the hook means the invariant "a bar is always painted" holds
   * for whatever fragment is added next, which is cheaper than the afternoon
   * spent finding out it did not. */
  document.addEventListener("htmx:afterSwap", function (event) {
    paintBars(event.target);
  });
})();
