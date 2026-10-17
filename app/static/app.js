/* Small progressive enhancement, so pages need no inline scripts or styles
 * (strict CSP). Without JavaScript the bars keep their default width and the
 * percentages are still in the text. */
(function () {
  "use strict";

  /* Bar widths come from data-pct (no inline style). */
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

  /* No swapped fragment has bars yet, but future ones will be handled. */
  document.addEventListener("htmx:afterSwap", function (event) {
    paintBars(event.target);
  });
})();
