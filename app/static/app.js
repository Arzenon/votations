/* Progressive enhancement, kept tiny and dependency-free.
 *
 * Everything here exists so the pages need no inline scripts and no inline
 * styles, which is what lets the Content-Security-Policy stay strict
 * (`script-src 'self'`, no 'unsafe-inline'). Without JavaScript the site still
 * works: bars simply render at their default width and confirmations are
 * skipped. */
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

  /* Destructive or billable actions confirm first, via data-confirm. */
  function wireConfirms(root) {
    var nodes = (root || document).querySelectorAll("[data-confirm]");
    Array.prototype.forEach.call(nodes, function (node) {
      if (node.dataset.confirmWired) return;
      node.dataset.confirmWired = "1";
      var handler = function (event) {
        if (!window.confirm(node.getAttribute("data-confirm"))) {
          event.preventDefault();
        }
      };
      if (node.tagName === "FORM") {
        node.addEventListener("submit", handler);
      } else {
        node.addEventListener("click", handler);
      }
    });
  }

  function init(root) {
    paintBars(root);
    wireConfirms(root);
  }

  document.addEventListener("DOMContentLoaded", function () {
    init(document);
  });
  /* HTMX swaps fragments in after load; re-run on whatever it inserted. */
  document.addEventListener("htmx:afterSwap", function (event) {
    init(event.target);
  });
})();
