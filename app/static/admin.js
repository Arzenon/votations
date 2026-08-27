/* Administration only — the public pages do not load this file.
 *
 * The public side is built to work with JavaScript switched off, because anyone
 * must be able to read a law and vote on one. The administration is a different
 * audience with a different bargain: a handful of known people, doing careful
 * editorial work, for whom a JSON textarea was a genuine obstacle. So this
 * enhances the argument editor rather than merely decorating it.
 *
 * It still degrades: the server renders every stored argument plus one blank
 * row, so with no JavaScript the form adds one argument per save and removing
 * one means emptying its three fields. Nothing here is required to edit a law —
 * it is required to edit a law *comfortably*.
 */
(function () {
  "use strict";

  var PLACEHOLDER = "__SIDE__";

  function template() {
    return document.getElementById("argument-row-template");
  }

  /* A fresh row, with the side stitched into the field names.
   *
   * The names carry no index on purpose (see _argument_row.html), so a new row
   * needs no renumbering — only the side it belongs to. */
  function newRow(side) {
    var row = template().content.firstElementChild.cloneNode(true);
    var fields = row.querySelectorAll("[name]");
    Array.prototype.forEach.call(fields, function (field) {
      field.name = field.name.replace(PLACEHOLDER, side);
    });
    return row;
  }

  /* One listener on the document rather than one per button: rows come and go,
   * and a listener attached to a row that is later removed is a listener that
   * has to be remembered. */
  document.addEventListener("click", function (event) {
    var add = event.target.closest(".add-argument");
    if (add) {
      var side = add.getAttribute("data-add");
      var list = add
        .closest(".arguments-editor")
        .querySelector(".argument-list");
      var row = newRow(side);
      list.appendChild(row);
      /* Put the cursor where the next thing typed belongs. */
      var first = row.querySelector("textarea, input");
      if (first) {
        first.focus();
      }
      return;
    }

    var remove = event.target.closest(".remove-argument");
    if (remove) {
      /* Removing the last row leaves the side empty, and that is allowed: a law
       * with no argument on one side is a fact the page is meant to be able to
       * state. « Ajouter un argument » is always there to start again. */
      remove.closest(".argument-row").remove();
    }
  });
})();
