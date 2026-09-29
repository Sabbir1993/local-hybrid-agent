import re
from collections import Counter


def is_degeneration_or_loop(text: str) -> tuple[bool, str]:
    if not text or len(text) < 70:
        return False, text

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) >= 3:
        for i in range(len(lines) - 2):
            if lines[i] == lines[i+1] == lines[i+2]:
                cut_idx = text.find(lines[i+1])
                if cut_idx != -1:
                    return True, text[:cut_idx].rstrip()
                return True, text

    sentences = [s.strip() for s in re.split(r'[.\n]+', text) if len(s.strip()) > 20]
    if len(sentences) >= 3:
        counts = Counter(sentences)
        for s, count in counts.items():
            if count >= 3:
                first_pos = text.find(s)
                second_pos = text.find(s, first_pos + len(s))
                if second_pos != -1:
                    return True, text[:second_pos].rstrip()
                return True, text

    for phrase_len in (25, 35, 50, 70):
        if len(text) > phrase_len * 3:
            phrase = text[-phrase_len:]
            if text.count(phrase) >= 3:
                first_pos = text.find(phrase)
                second_pos = text.find(phrase, first_pos + phrase_len)
                if second_pos != -1:
                    return True, text[:second_pos].rstrip()
                return True, text

    return False, text


def is_repeating_loop(text: str) -> bool:
    loop_detected, _ = is_degeneration_or_loop(text)
    return loop_detected


def sanitize_user_facing_content(text: str) -> str:
    if not text:
        return ""
    is_loop, cleaned = is_degeneration_or_loop(text)
    text = cleaned if is_loop else text
    text = re.sub(r"<tool_call>[\s\S]*?</tool_call>", "", text)
    text = re.sub(r'<function\s+name=["\'][\s\S]*?</function>', "", text)
    text = re.sub(r"</?(?:tool_call|function|param|tool_response|tool_sep|im_start|im_end)>", "", text)
    text = text.replace("<![CDATA[", "").replace("]]>", "")
    text = re.sub(r"```(?:json)?\s*\{\s*\"name\"\s*:\s*\"(?:write_file|edit_file|read_file|list_files|run_python)\"[\s\S]*?\}\s*```", "", text)
    return text.strip()
