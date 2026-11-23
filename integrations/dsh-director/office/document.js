// Step 24.2d (design-steward 7.8.12c "四"): the Document Builder template for the steward's pdf
// and docx. The host puts ONE line before it - `var DATA = <JSON>;`, a data literal
// (lib/office-blocks.mjs dataLine) - and its own fixed save lines after it, and runs the result
// with OnlyOffice's docbuilder in a disposable container without network. docbuilder takes its
// own commands only as whole top-level lines, so those are the host's; this file only reads
// DATA: it builds no code from it (no eval, no Function), takes no path from it, fetches nothing.
//
// DATA (built and checked by the plugin): {format: "pdf" | "docx", title, font, blocks: [...]}.
// A block: {t: "h", level: 1-3, runs} | {t: "p", runs, align?: "right"} |
//   {t: "li", mark: "•" | "3.", runs} | {t: "table", rows: [[runs...]...]} | {t: "hr"} | {t: "pagebreak"}
// A run: {text, b?: true, i?: true}.
builder.CreateFile("docx");
(function () {
  // Half-points: body 11 pt, headings 16, 14 and 12 pt. Twips: A4, margins 2.5 cm.
  var BODY = 22, HEADING = [0, 32, 28, 24];
  var A4_W = 11906, A4_H = 16838, MARGIN = 1418;
  var doc = Api.GetDocument();
  function addRuns(paragraph, runs, extra) {
    for (var i = 0; i < runs.length; i++) {
      var run = Api.CreateRun();
      run.AddText(runs[i].text);
      run.SetFontFamily(DATA.font);
      if (runs[i].b || (extra && extra.b)) run.SetBold(true);
      if (runs[i].i) run.SetItalic(true);
      if (extra && extra.size) run.SetFontSize(extra.size);
      paragraph.AddElement(run);
    }
  }
  var section = doc.GetFinalSection();
  section.SetPageSize(A4_W, A4_H);
  section.SetPageMargins(MARGIN, MARGIN, MARGIN, MARGIN);
  var defaults = doc.GetDefaultTextPr();
  defaults.SetFontFamily(DATA.font);
  defaults.SetFontSize(BODY);
  try { doc.GetCore().SetTitle(DATA.title); } catch (e) { /* a builder without core properties */ }
  // A new document holds one empty paragraph: the first block takes its place.
  var fresh = doc.GetElementsCount() === 1;
  function push(element) {
    doc.Push(element);
    if (fresh) { doc.RemoveElement(0); fresh = false; }
  }
  for (var i = 0; i < DATA.blocks.length; i++) {
    var block = DATA.blocks[i], p;
    if (block.t === "h") {
      p = Api.CreateParagraph();
      p.SetSpacingBefore(240);
      p.SetSpacingAfter(120);
      p.SetKeepNext(true);
      addRuns(p, block.runs, { b: true, size: HEADING[block.level] });
      push(p);
    } else if (block.t === "p") {
      p = Api.CreateParagraph();
      p.SetSpacingAfter(120);
      if (block.align === "right") p.SetJc("right");
      addRuns(p, block.runs);
      push(p);
    } else if (block.t === "li") {
      p = Api.CreateParagraph();
      p.SetIndLeft(567);
      p.SetIndFirstLine(-340);
      p.SetSpacingAfter(60);
      addRuns(p, [{ text: block.mark + " " }].concat(block.runs));
      push(p);
    } else if (block.t === "hr") {
      p = Api.CreateParagraph();
      p.SetBottomBorder("single", 6, 0, 128, 128, 128);
      push(p);
    } else if (block.t === "pagebreak") {
      p = Api.CreateParagraph();
      p.AddPageBreak();
      push(p);
    } else if (block.t === "table") {
      var rows = block.rows.length, cols = block.rows[0].length;
      var table = Api.CreateTable(cols, rows);
      table.SetWidth("percent", 100);
      var sides = ["SetTableBorderTop", "SetTableBorderBottom", "SetTableBorderLeft", "SetTableBorderRight",
        "SetTableBorderInsideH", "SetTableBorderInsideV"];
      for (var s = 0; s < sides.length; s++) table[sides[s]]("single", 8, 0, 0, 0, 0);
      for (var r = 0; r < rows; r++) {
        for (var c = 0; c < cols; c++) {
          var cell = table.GetRow(r).GetCell(c);
          if (r === 0) cell.SetShd("clear", 230, 230, 230);
          addRuns(cell.GetContent().GetElement(0), block.rows[r][c], r === 0 ? { b: true } : undefined);
        }
      }
      push(table);
      // A paragraph after a table keeps the next table apart from it.
      push(Api.CreateParagraph());
    }
  }
})();
