DEVICE_MAX_BYTES = 10 * 1024 * 1024   # companion frames (uvicorn ws_max_size 16 MB, base64)

_OPS_HELP = (
    "List of edit ops applied in place; everything not targeted stays byte-identical. "
    "Addresses come from doc_inspect. "
    "PPTX: set_text{addr:'s2/sh3'|'s2/title'|'s2/sh3/p1'|'s2/sh5/tbl[1,2]',text}, replace_text{find,replace,scope?:'s2'}, "
    "add_slide{after:2,title,bullets:[..],layout?,like?:'s3'}, duplicate_slide{addr:'s2'}, delete_slide{addr:'s4'}, "
    "move_slide{addr:'s4',to:2}, set_notes{addr:'s2',text}, replace_image{addr:'s2/sh4',image:'logo.png'}. "
    "XLSX: set_column{sheet?,column:'Cost (BDT)'|'C',values:[..] or formula:'=B{r}*Rate',rows?} changes ONE column of ONE sheet "
    "(found by its header) and nothing else, set_cell{addr:'Sheet1!B7',value}, set_formula{addr,formula:'=SUM(B2:B6)'}, "
    "set_range{addr:'Sheet1!A2',values:[[..],[..]],formulas?:true}, clear{addr:'Sheet1!B2:C4'}, "
    "append_rows{sheet,rows:[[..]]}, insert_rows{sheet,at,count,values?}, delete_rows{sheet,at,count}, "
    "add_sheet{name,rows?}, rename_sheet{sheet,to}. "
    "XLSX built here from an outline can also be extended / restyled (do not mix with cell ops): add_outline_sheet{name,content:"
    "'| Col | Col |\n|---|---|\n| a | b |\nformat: ..'} (one more sheet, same syntax as in doc_create), add_chart{sheet?,chart:'bar | title=.. | "
    "x=Col | y=Col'}, set_format{sheet?,column,format:'currency:BDT|percent|date|int'}, add_highlight{sheet?,rule:'Col<0 red'}, "
    "add_scale{sheet?,column}, add_databar{column}, add_validation{sheet?,rule:'Col in [a, b]'}, set_total{sheet?,column,fn:'sum'}, "
    "set_theme{theme}, set_freeze{sheet?,cell}, set_table{sheet?,name}, add_note{sheet?,text}, add_column{sheet?,name,formula:"
    "'=C{r}*Rate'}. "
    "CSV: set_cell{addr:'r3cAmount'|'r3c2'|'B3',value}, set_row{row,values}, append_rows{rows}, "
    "insert_rows{at,rows}, delete_rows{at,count}, add_column{name,values}, replace_text{find,replace}. "
    "DOCX: set_text{addr:'p4'|'t1[2,3]'|'hdr1/p1',text}, insert_paragraph_after{after:'p4',text|texts,style?}, "
    "delete_paragraph{addr}, add_table_row{addr:'t1',values,after?}, replace_text{find,replace}. "
    "PDF (generated here) / MD: replace_section{addr:'sec2',text}, set_heading{addr,text}, "
    "insert_section_after{after:'sec2',text}, delete_section{addr}, replace_text{find,replace}."
)

DOC_INSPECT_SCHEMA = {"type": "function", "function": {
    "name": "doc_inspect",
    "description": "Read a PowerPoint/Excel/Word/CSV/PDF document as a structured outline with stable "
                   "addresses (slide shapes, cells, paragraphs, sections). Call before doc_edit.",
    "parameters": {"type": "object", "properties": {
        "file": {"type": "string", "description": "file name, e.g. deck.pptx"},
        "focus": {"type": "string", "description": "optional address to zoom into, e.g. s4 or Sheet1!row20"},
    }, "required": ["file"]}}}

DOC_EDIT_SCHEMA = {"type": "function", "function": {
    "name": "doc_edit",
    "description": "Change only the requested parts of an existing PowerPoint/Excel/Word/CSV/PDF document, "
                   "keeping all other content, layout, styles and formatting exactly as they are. Use this "
                   "instead of regenerating a document. Saves a new version; the old one is kept.",
    "parameters": {"type": "object", "properties": {
        "file": {"type": "string"},
        "ops": {"type": "array", "items": {"type": "object"}, "description": _OPS_HELP},
        "in_place": {"type": "boolean", "description": "agent mode only: overwrite the file instead of saving -vN"},
    }, "required": ["file", "ops"]}}}

DOC_CREATE_SCHEMA = {"type": "function", "function": {
    "name": "doc_create",
    "description": "Create a new .pptx/.docx/.xlsx/.csv/.pdf from markdown. For .pptx use an outline: 'theme: "
                   "midnight|corporate|forest|sunset|minimal|royal', '# Deck title', 'subtitle:', then slides separated "
                   "by '---', each '## Title' + 3-5 short bullets (optional 'layout: cards|stats|timeline|two-column|"
                   "quote|section', 'notes:'). For .xlsx write a workbook outline: '# Title', 'theme:', then one '## Sheet: Name' + a "
                   "markdown table per sheet; formulas are real when written as =SUM(B2:B9) in a cell (checked and calculated for "
                   "you), and lines under a table set 'format: Col=currency:BDT|percent|date|int', 'total: Col=sum', "
                   "'highlight: Col<0 red', 'scale: Col', 'validate: Col in [a, b]', 'table: Name', 'freeze: A2', 'name: Rate = "
                   "Assumptions!B2', 'chart: bar|line|pie|column|scatter | title=.. | x=Col | y=Col[,Col]'. Tables as markdown tables or CSV. "
                   "For presentations (.pptx), provide the complete slide outline with all slides in one single doc_create call. "
                   "You may optionally pass total=<N> to build very large multi-section decks in pieces with doc_append. "
                   "Built so later doc_edit calls can change parts of it.",
    "parameters": {"type": "object", "properties": {
        "file": {"type": "string", "description": "file name with extension"},
        "content": {"type": "string", "description": "markdown / table text"},
        "total": {"type": "integer", "description": "how many slides (pptx) or sections (pdf/docx) the finished "
                                                    "document will have; set it when building in pieces"},
        "template": {"type": "string", "description": "optional .pptx whose theme and layouts to use"},
        "plan": {"type": "string", "description": "optional, when building in pieces: your plan, one line per slide / "
                                                   "section, '1. Title - purpose'. It is returned to you after every "
                                                   "doc_append so you stay on it"},
    }, "required": ["file", "content"]}}}

DOC_APPEND_SCHEMA = {"type": "function", "function": {
    "name": "doc_append",
    "description": "Add the NEXT 2-3 slides (.pptx) or sections (.pdf/.docx) to a document you are building in pieces "
                   "and rebuild it in place; the user watches it grow in the preview pane. Use the exact file name "
                   "doc_create returned. Same markdown format as doc_create (slides separated by '---', each "
                   "'## Title' + 3-5 short bullets, plain text only). The last slide is the closing slide. Set "
                   "done=true with the final call, never earlier.",
    "parameters": {"type": "object", "properties": {
        "file": {"type": "string", "description": "the file name returned by doc_create"},
        "content": {"type": "string", "description": "markdown for the next 2-3 slides / sections only"},
        "done": {"type": "boolean", "description": "true on the last call: finishes the document"},
    }, "required": ["file", "content"]}}}

DOC_SCHEMAS = [DOC_INSPECT_SCHEMA, DOC_EDIT_SCHEMA, DOC_CREATE_SCHEMA, DOC_APPEND_SCHEMA]
