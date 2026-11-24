// Step 24.2d (design-steward 7.8.12c "四"): the Document Builder template for the steward's xlsx.
// As document.js: the host puts `var DATA = <JSON>;` before it and its own fixed save lines after
// it; this file only reads DATA.
//
// DATA (built and checked by the plugin): {format: "xlsx", title, font, sheet, widths: [chars...],
//   rows: [[cell...]...]}, the first row the header. A cell: {s: text} | {n: number} |
//   {f: "=SUM(A1:A3)"} (a formula of the plugin's whitelist).
builder.CreateFile("xlsx");
(function () {
  var sheet = Api.GetActiveSheet();
  sheet.SetName(DATA.sheet);
  for (var r = 0; r < DATA.rows.length; r++) {
    for (var c = 0; c < DATA.rows[r].length; c++) {
      var cell = DATA.rows[r][c], range = sheet.GetRangeByNumber(r, c);
      range.SetFontName(DATA.font);
      if (cell.f !== undefined) range.SetValue(cell.f);
      else if (cell.n !== undefined) range.SetValue(cell.n);
      else {
        // Text stays text: the cell is formatted as text before its value is set, so a value
        // that starts with = + - @ is never read as a formula (7.8.12c "四").
        range.SetNumberFormat("@");
        range.SetValue(cell.s);
      }
      if (r === 0) range.SetBold(true);
    }
  }
  for (var w = 0; w < DATA.widths.length; w++) sheet.SetColumnWidth(w, DATA.widths[w]);
  try { sheet.GetFreezePanes().FreezeRows(1); } catch (e) { /* a builder without freeze panes */ }
})();
