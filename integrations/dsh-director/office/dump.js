// Step 24.2d (design-steward 7.8.12c "五"): the Document Builder template that reads an xlsx for
// read_document. The host puts `var DATA = <JSON>;` before it (lib/office-blocks.mjs dataLine)
// and runs it in the same disposable container as document.js; the file is always
// /job/in/src.xlsx, opened by the host's own first line. This file only reads DATA and writes ONE
// line to stdout: "OFFICE_XLSX " + JSON {sheet_count, sheets: [{n, name, rows_total,
// cols_total, first_row, rows: [[text...]...]}]} - each cell's shown text (a formula's cached
// value), cut at DATA.cell_chars; sheets DATA.first..DATA.last (from 1), rows from DATA.row_first,
// at most DATA.rows per sheet and DATA.cols columns, stopping at a whole row once DATA.chars
// characters were given (always at least one row).
(function () {
  function column(n) { var s = ""; n += 1; while (n > 0) { var m = (n - 1) % 26; s = String.fromCharCode(65 + m) + s; n = Math.floor((n - 1) / 26); } return s; }
  function cellIndex(ref) {
    var m = /^([A-Z]+)(\d+)$/.exec(ref);
    if (!m) return null;
    var c = 0;
    for (var i = 0; i < m[1].length; i++) c = c * 26 + (m[1].charCodeAt(i) - 64);
    return { row: Number(m[2]), col: c };
  }
  var sheets = Api.GetSheets(), out = { sheet_count: sheets.length, sheets: [] }, given = 0;
  for (var n = DATA.first; n <= DATA.last && n <= sheets.length; n++) {
    var sheet = sheets[n - 1], used = sheet.GetUsedRange();
    var address = String(used.GetAddress(false, false, "xlA1", false)).replace(/\$/g, "").split(":");
    var from = cellIndex(address[0]), to = cellIndex(address[address.length - 1]) || from;
    var entry = { n: n, name: String(sheet.GetName()), rows_total: 0, cols_total: 0, first_row: DATA.row_first, rows: [] };
    if (from && to) {
      // The used range as rows and columns counted from A1, so row numbers are the sheet's own.
      entry.rows_total = to.row;
      entry.cols_total = to.col;
      var lastRow = Math.min(to.row, DATA.row_first + DATA.rows - 1), lastCol = Math.min(to.col, DATA.cols);
      if (DATA.row_first <= lastRow) {
        var range = sheet.GetRange("A" + DATA.row_first + ":" + column(lastCol - 1) + lastRow);
        var text = range.GetText();
        if (!(text instanceof Array)) text = [[text]];
        for (var r = 0; r < text.length; r++) {
          var line = text[r] instanceof Array ? text[r] : [text[r]], row = [], size = 0;
          for (var c = 0; c < line.length && c < DATA.cols; c++) {
            var value = line[c] === undefined || line[c] === null ? "" : String(line[c]);
            if (value.length > DATA.cell_chars) value = value.slice(0, DATA.cell_chars);
            row.push(value);
            size += value.length;
          }
          if (entry.rows.length && given + size > DATA.chars) break;
          if (!entry.rows.length && out.sheets.length && given + size > DATA.chars) break;
          entry.rows.push(row);
          given += size;
        }
      }
    }
    out.sheets.push(entry);
    if (given >= DATA.chars) break;
  }
  console.log("OFFICE_XLSX " + JSON.stringify(out));
})();
