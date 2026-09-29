DEVICE_MAX_BYTES = 10 * 1024 * 1024   # companion frames (uvicorn ws_max_size 16 MB, base64)

_OPS_HELP = (
    "List of edit ops applied in place; everything not targeted stays byte-identical. "
    "Addresses come from doc_inspect. "
    "PPTX: set_text{addr:'s2/sh3'|'s2/title'|'s2/sh3/p1'|'s2/sh5/tbl[1,2]',text}, replace_text{find,replace,scope?:'s2'}, "
    "add_slide{after:2,title,bullets:[..],layout?,like?:'s3'}, duplicate_slide{addr:'s2'}, delete_slide{addr:'s4'}, "
    "move_slide{addr:'s4',to:2}, set_notes{addr:'s2',text}, replace_image{addr:'s2/sh4',image:'logo.png'}. "
    "XLSX: set_cell{addr:'Sheet1!B7',value}, set_formula{addr,formula:'=SUM(B2:B6)'}, "
    "set_range{addr:'Sheet1!A2',values:[[..],[..]],formulas?:true}, clear{addr:'Sheet1!B2:C4'}, "
    "append_rows{sheet,rows:[[..]]}, insert_rows{sheet,at,count,values?}, delete_rows{sheet,at,count}, "
    "add_sheet{name,rows?}, rename_sheet{sheet,to}. "
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
    "description": "Create a new .pptx/.docx/.xlsx/.csv/.pdf from markdown (slides split by '---' or '# '; "
                   "tables as markdown tables or CSV). Built so later doc_edit calls can change parts of it.",
    "parameters": {"type": "object", "properties": {
        "file": {"type": "string", "description": "file name with extension"},
        "content": {"type": "string", "description": "markdown / table text"},
        "template": {"type": "string", "description": "optional .pptx whose theme and layouts to use"},
    }, "required": ["file", "content"]}}}

DOC_SCHEMAS = [DOC_INSPECT_SCHEMA, DOC_EDIT_SCHEMA, DOC_CREATE_SCHEMA]
