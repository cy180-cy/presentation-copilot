"""Small deterministic checks; no model calls and no image-content verification."""
import copy
import hashlib
import json
import re
from difflib import SequenceMatcher

CONTENT_FIELDS = ('text', 'title', 'text_content', 'extra_fields', 'layout_suggestion')


def allocate_visible_text(description, page_title=None, extra_requirements=None):
    """Annotate simple, explicit copy without inventing or rewriting any text.

    One matching heading and one continuous ordered list are also unambiguous.
    A list marker has one owner: a visual badge OR the inline list prefix.
    Complex Markdown and intentional expansion/repetition keep their semantics.
    This is prompt-only; saved descriptions are untouched.
    """
    start, end = '--- 页面文字 ---', '--- 页面文字结束 ---'
    instructions = description + '\n' + (extra_requirements or '')
    if re.search(r'允许重复|刻意重复|重复展示|扩写|扩展|新增|补充.{0,12}(?:建议|要点|条目|行动)|'
                 r'(?:保留|显示|展示).{0,12}(?:符号|字符|标记|#)|'
                 r'\b(?:keep|show|display)\b.{0,30}(?:#|\b(?:literal|symbols?|markers?)\b)|'
                 r'\b(?:expand|elaborate)\b|\badd\b.{0,30}\b(?:points?|suggestions?|items?)\b',
                 instructions, re.I):
        return description
    if description.count(start) == 1 and description.count(end) == 1:
        prefix, rest = description.split(start, 1)
        if end not in rest:
            return description
        body, suffix = rest.split(end, 1)
    elif re.match(r'^页面文字[：:]\s*\n', description.strip()) and start not in description and end not in description:
        # Hand-saved simple copy, as used by PC-008.1. Unknown sections/fields
        # remain untouched instead of counting layout instructions as actions.
        body = description.strip().split('\n', 1)[1]
        prefix, suffix = '', ''
        if re.search(r'[：:]', body):
            return description
    else:
        return description
    raw_lines = [line for line in body.splitlines() if line.strip()]
    lines = [line.strip() for line in raw_lines]
    if not page_title or not 1 <= len(lines) <= 24:
        return description
    # Only the explicit heading matching the page title loses its format marker.
    # Other # characters (C#, #include, literal symbols) are never stripped.
    headings = [i for i, line in enumerate(lines)
                if line == page_title or re.fullmatch(r'#{1,6}[ \t]+' + re.escape(page_title), line)]
    if len(headings) != 1:
        return description
    heading = headings[0]
    ordered = [re.fullmatch(r'([1-9]\d*)(\.(?!\d)|[)、])[ \t]*(\S.*)', line)
               for line in lines[heading + 1:]]
    is_ordered = bool(ordered) and any(ordered)
    if is_ordered:
        # A single number, mixed groups, nested/indented or discontinuous lists
        # are ambiguous. Keep them intact rather than renumbering user content.
        if (heading not in (0, 1) or (heading == 1 and lines[0] != '演示材料') or
                len(ordered) < 2 or not all(ordered) or
                any(raw != raw.lstrip() for raw in raw_lines[heading + 1:]) or
                [int(match[1]) for match in ordered] != list(range(1, len(ordered) + 1)) or
                len({match[2] for match in ordered}) != 1):
            return description
        content = [match[3] for match in ordered]
    else:
        content = [line for i, line in enumerate(lines) if i != heading]
    if any(len(line) > 120 or re.search(r'[*`|$<>=\[\]]|https?://|^[-+]\s|^#|^\d+[.)、]\s*', line)
           for line in content):
        return description
    lines[heading] = page_title
    items = []
    for index, line in enumerate(lines):
        role = 'heading' if line == page_title else 'key_message'
        if line == '演示材料' and line != page_title:
            role = 'small_label'
        item = {'role': role, 'text': line}
        if is_ordered and index > heading:
            match = ordered[index - heading - 1]
            # Separate only a validated consecutive list prefix. Business
            # numbers within the sentence, saved copy and layout stay intact.
            item.update(text=match[3], list_marker=match[1] + match[2])
        items.append(item)
    count = sum(item['role'] == 'key_message' for item in items)
    if not count:
        return description
    # Each source string appears only here, not again in a second copy inventory.
    return (prefix + start + '\n<visible_copy>\n' +
            json.dumps(items, ensure_ascii=False, separators=(',', ':')) +
            '\n</visible_copy>\n' + end +
            f'\n<content_constraints business_item_count="{count}">'
            '业务条目仅限上述 key_message，数量必须等于 business_item_count；heading 和 small_label 不计入业务条目。'
            '逐字保留原句、数字和单位，不补造行动项；模板多余位置可合并、减少或留白。'
            + ('list_marker 是对应条目的唯一列表序号，text 不含此前缀。'
               '模板已有序号徽章或版式要求独立大号序号时，将 list_marker 的数字用于该位置，正文仅显示 text；'
               '没有这类序号位置时，将 list_marker 放在对应 text 行首，保留原编号及分隔符。'
               '两种位置互斥，每项序号仅显示一次，不再给正文、图标或其他装饰追加同一序号。'
               '正文中的年份、数值、版本号及单位不是列表序号，必须原样保留。'
               '整页只安排一套完整列表，不在其他区域重复章节。'
               if is_ordered else '') +
            '本段及属性是控制说明，不渲染。无独立补充文字时不增设正文层。'
            '</content_constraints>' + suffix)


def normalize_visible_text(description):
    """Deduplicate exact short visible lines only; warn, never merge near matches."""
    start, end = '--- 页面文字 ---', '--- 页面文字结束 ---'
    if start not in description or end not in description:
        # Legacy text_content arrays are joined without markers. Warn only:
        # without a visible-text boundary, deleting a line could remove layout.
        _, warnings = normalize_visible_text(start + '\n' + description + '\n' + end)
        return description, warnings
    prefix, rest = description.split(start, 1)
    body, suffix = rest.split(end, 1)
    repeat_intended = bool(re.search(r'允许重复|刻意重复|重复展示', description))
    seen, output, warnings = [], [], []
    in_code = False
    for line in body.splitlines():
        if line.strip().startswith('```'):
            in_code = not in_code
        plain = re.sub(r'^\s*(?:#{1,6}\s+|[-*+]\s+)', '', line).strip()
        key = re.sub(r'\s+', '', plain)
        if not in_code and 4 <= len(key) <= 160 and not any(x in line for x in ('|', '![', 'http', '$')):
            if key in seen:
                warnings.append('exact_duplicate')
                if not repeat_intended:
                    continue
            elif len(seen) < 200 and any(
                SequenceMatcher(None, key, old).ratio() >= .88 for old in seen
            ):
                warnings.append('near_duplicate')
            if len(seen) < 200:
                seen.append(key)
        output.append(line)
    return prefix + start + '\n'.join(output) + '\n' + end + suffix, sorted(set(warnings))


def _content(description):
    return {k: description[k] for k in CONTENT_FIELDS if k in description}


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def content_fingerprint(description, outline):
    data = json.dumps([_content(description or {}), outline], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(data.encode()).hexdigest()


def stamp_consistency(page, state):
    description = copy.deepcopy(page.get_description_content() or {})
    description['_image_consistency'] = {
        'state': state, 'image_path': page.generated_image_path,
        'content_hash': content_fingerprint(description, page.get_outline_content()),
    }
    page.set_description_content(description)


def consistency_state(page):
    if not page.generated_image_path:
        return 'no_image'
    description = page.get_description_content() or {}
    record = description.get('_image_consistency') or {}
    if (record.get('image_path') != page.generated_image_path or
            record.get('content_hash') != content_fingerprint(description, page.get_outline_content())):
        return 'needs_review'
    return record.get('state', 'needs_review')


def plan_edit_sync(description, outline, instruction):
    """Single literal replacement, not semantic interpretation or OCR.

    Numbers propagate only inside the exact phrase or the quoted number with
    its same unit/context suffix. Other occurrences make the edit ambiguous.
    """
    description, outline = copy.deepcopy(description or {}), copy.deepcopy(outline or {})
    instruction = instruction.strip().rstrip('。.!')
    visual = r'(?:将|把)?(?:背景|背景颜色|文字颜色|字体颜色|标题颜色)(?:改为|改成|换成)(?:红色|蓝色|绿色|白色|黑色|黄色)(?:[，,]其他(?:文字|内容)(?:保持)?不变)?'
    if re.fullmatch(visual, instruction):
        return description, outline, 'visual_only'
    # A scoped number replacement: the entire context must occur exactly once
    # in visible copy. Never replace the number globally (15%, 2015, IDs, etc.).
    scoped = re.fullmatch(
        r'(?:将|把)[“「\"]?([^\n，,；;。]+?)[”」\"]?中的[“「\"]?(\d+(?:\.\d+)?)[”」\"]?'
        r'(?:改为|改成|替换为)[“「\"]?(\d+(?:\.\d+)?)[”」\"]?'
        r'(?:[，,]\s*(?:其余文字和整体版式尽量保持不变|其他文字和整体风格尽量保持一致))?', instruction)
    if scoped:
        context, before, after = scoped.groups()
        boundary = r'(?<![\d.])' + re.escape(before) + r'(?![\d.%％])'
        visible = '\n'.join(_strings({k: description[k] for k in ('text', 'title', 'text_content') if k in description}))
        if (len(context) < 4 or before == after or visible.count(context) != 1 or
                len(re.findall(boundary, context)) != 1 or
                any(x in context for x in ('http', '/files/', '同时', '然后', '并且', '中的'))):
            return description, outline, 'needs_review'
        updated = re.sub(boundary, after, context)
        values = list(_strings(_content(description))) + list(_strings(outline))
        if any(v.count(context) > 1 for v in values):
            return description, outline, 'needs_review'
        # Only this exact phrase, plus an explicitly quoted number with the same
        # unit/context suffix in descriptive layout fields, can change.
        suffix = context.split(before, 1)[1]
        quoted = re.compile('([“「\"])' + re.escape(before) + '([”」\"])' + re.escape(suffix)) if suffix else None
        def scoped_replace(value, layout=False):
            if isinstance(value, str):
                value = value.replace(context, updated)
                if layout and quoted:
                    value = quoted.sub(lambda m: m[1] + after + m[2] + suffix, value)
                return value
            if isinstance(value, list):
                return [scoped_replace(v, layout) for v in value]
            if isinstance(value, dict):
                return {k: scoped_replace(v, layout) for k, v in value.items()}
            return value
        outline_match = any(context in v for v in _strings(outline))
        for key in CONTENT_FIELDS:
            if key in description:
                description[key] = scoped_replace(description[key], key in ('extra_fields', 'layout_suggestion'))
        return description, scoped_replace(outline), 'synced_by_instruction' if outline_match else 'needs_review'
    match = re.fullmatch(
        r'(?:将|把)(.+?)(?:改为|改成|替换为)(.+?)(?:[，,]其他文字和整体风格尽量保持一致)?', instruction)
    if not match:
        return description, outline, 'needs_review'
    old, new = [x.strip().strip('“”「」\"') for x in match.groups()]
    visible = '\n'.join(_strings({k: description[k] for k in ('text', 'title', 'text_content') if k in description}))
    # Reject bare numbers, multiple edits, prose continuations and ambiguous locations.
    if (len(old) < 4 or len(new) < 2 or visible.count(old) != 1 or old == new or
            any(x in old + new for x in ('\n', '，', ',', '；', ';', '。', '改为', '替换为', '然后', '同时', '并且', 'http', '/files/'))):
        return description, outline, 'needs_review'
    old_numbers, new_numbers = re.findall(r'\d+(?:\.\d+)?', old), re.findall(r'\d+(?:\.\d+)?', new)
    number_change = (len(old_numbers) == len(new_numbers) == 1 and
                     re.sub(r'\d+(?:\.\d+)?', '#', old) == re.sub(r'\d+(?:\.\d+)?', '#', new))
    all_values = list(_strings(_content(description))) + list(_strings(outline))
    if any('http' in s or '/files/' in s for s in all_values):
        return description, outline, 'needs_review'
    if number_change:
        numbers = set(re.findall(r'\d+(?:\.\d+)?', '\n'.join(all_values)))
        if numbers != {old_numbers[0]}:
            return description, outline, 'needs_review'
        suffix = old.split(old_numbers[0], 1)[1]
        if not suffix:
            return description, outline, 'needs_review'
        quoted = re.compile('[“「\"]' + re.escape(old_numbers[0]) + '[”」\"]' + re.escape(suffix))
        number_pattern = r'(?<![\d.])' + re.escape(old_numbers[0]) + r'(?![\d.])'
        for value in all_values:
            remainder = quoted.sub('', value.replace(old, ''))
            if re.search(number_pattern, remainder):
                return description, outline, 'needs_review'
    def replace(value):
        if isinstance(value, str):
            if number_change:
                return re.sub(number_pattern, new_numbers[0], value)
            return value.replace(old, new)
        if isinstance(value, list):
            return [replace(v) for v in value]
        if isinstance(value, dict):
            return {k: replace(v) for k, v in value.items()}
        return value
    for key in CONTENT_FIELDS:
        if key in description:
            description[key] = replace(description[key])
    return description, replace(outline), 'synced_by_instruction'
