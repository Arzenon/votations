/* Admin only: adding and removing argument rows.
 *
 * Without JavaScript the form still works: arguments are removed by emptying
 * their fields.
 */
(function () {
  "use strict";

  var PLACEHOLDER = "__SIDE__";

  function template() {
    return document.getElementById("argument-row-template");
  }

  /* A new row for the given side. Field names have no index (see
   * _argument_row.html), so nothing to renumber. */
  function newRow(side) {
    var row = template().content.firstElementChild.cloneNode(true);
    var fields = row.querySelectorAll("[name]");
    Array.prototype.forEach.call(fields, function (field) {
      field.name = field.name.replace(PLACEHOLDER, side);
    });
    return row;
  }

  /* A single delegated listener, since rows come and go. */
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
      /* A side may be left empty. */
      remove.closest(".argument-row").remove();
    }
  });
})();
