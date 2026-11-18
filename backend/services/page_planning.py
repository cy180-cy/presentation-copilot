"""Deterministic page targets and conservative, server-owned admission."""
import re

DEFAULT_AUTO_PAGES = 5
PLANNING_OPERATIONS = {'generate_outline', 'generate_outline_stream', 'generate_from_description'}
PROMPT_VERSION = 'pc0102-soft-page-limits-v1'
PLAN_FIELDS = ('requested_pages', 'default_auto_pages', 'planned_page_count', 'max_pages', 'page_count_source',
               'target_pages', 'resolved_pages', 'default_pages', 'affordable_pages_at_start', 'page_limit_reason')


class PagePlanningError(ValueError):
    pass


_CHINESE_DIGITS = dict(zip('零〇一二两三四五六七八九', (0, 0, 1, 2, 2, 3, 4, 5, 6, 7, 8, 9)))
_CHINESE_UNITS = {'十': 10, '百': 100, '千': 1000, '万': 10000}
_CHINESE_NUMERALS = ''.join(_CHINESE_DIGITS) + ''.join(_CHINESE_UNITS)


def _page_quantity(value):
    if value.isascii():
        return int(value) if len(value) <= 4 else 10000
    total, digit, previous_unit = 0, None, 100000
    for char in value:
        if char in _CHINESE_DIGITS:
            # Consecutive nonzero digits (e.g. 两三页) are not an exact count.
            if digit not in (None, 0):
                return None
            digit = _CHINESE_DIGITS[char]
        else:
            unit = _CHINESE_UNITS[char]
            if unit >= previous_unit or digit == 0:
                return None
            if digit is None:
                if unit != 10 or total:
                    return None
                digit = 1
            total += digit * unit
            digit, previous_unit = None, unit
    return total + (digit or 0)


def requested_pages(text):
    # Only explicit quantities with page units. Ordinals, ranges and qualified
    # quantities are not exact requests; unrelated business numbers are ignored.
    matches = list(re.finditer(r'(?<![\w.第负\-])([0-9]+)\s*(?:页|pages?\b|slides?\b)', text, re.I))
    # Chinese words have no spaces, so allow them before a number except 第/负.
    matches += list(re.finditer(r'(?<=[\u4e00-\u9fff])([0-9]+)\s*页', text))
    matches += list(re.finditer(
        rf'(?<![0-9.{_CHINESE_NUMERALS}第负点\-])([{_CHINESE_NUMERALS}]+)\s*页', text))
    candidates = {}
    for match in matches:
        before, after = text[:match.start()], text[match.end():]
        if re.search(r'(?:第|负)\s*$', before):
            continue
        if (re.search(rf'(?:至少|最多|大约|约|不超过|不少于|不多于|第|负|[\d.{_CHINESE_NUMERALS}]\s*[-–~～至到])\s*$', before)
                or re.match(r'\s*(?:以内|以上|以下|左右|起|[-–~～至到或])', after)
                or re.search(r'(?:at least|at most|about|up to)\s*$', before, re.I)):
            return None
        value = _page_quantity(match.group(1))
        if value is None:
            return None
        candidates[match.start()] = value
    values = set(candidates.values())
    return next(iter(values)) if len(values) == 1 else None


def resolve(text, limits, affordable_pages=None):
    default, maximum = limits.get('default_auto_pages', DEFAULT_AUTO_PAGES), limits['pages']
    if type(default) is not int or not 1 <= default <= 50 or type(maximum) is not int or not 1 <= maximum <= 50:
        raise PagePlanningError('页数设置无效，请联系开发者检查。')
    explicit = requested_pages(text)
    if explicit is not None and explicit < 1:
        raise PagePlanningError('页数至少为1页，请调整后再生成。')
    target = explicit if explicit is not None else default
    affordable = maximum if affordable_pages is None else min(maximum, affordable_pages)
    resolved = min(target, maximum, affordable)
    if resolved < 1:
        raise PagePlanningError('当前额度不足以完成1页，未开始生成；已有作品仍可使用。')
    hard, budget = target > maximum, affordable < min(target, maximum)
    reason = 'max_pages_and_budget' if hard and budget else 'max_pages' if hard else 'budget' if budget else 'none'
    return {'requested_pages': explicit, 'default_auto_pages': default,
            'planned_page_count': resolved, 'target_pages': target, 'resolved_pages': resolved,
            'default_pages': default, 'affordable_pages_at_start': affordable, 'page_limit_reason': reason,
            'max_pages': maximum, 'page_count_source': 'user_explicit' if explicit is not None else 'default_auto'}


def for_project(project, body, limits, affordable_pages=None):
    field = {'outline': 'outline_text', 'descriptions': 'description_text'}.get(project.creation_type, 'idea_prompt')
    # Only the editable source and explicit outline requirements, not attachments,
    # template examples or previous generated pages, determine requested count.
    source = body.get(field) or getattr(project, field, '') or ''
    return resolve(source + '\n' + (project.outline_requirements or ''), limits, affordable_pages)


def preflight(project_id, body):
    """Validate and hold a workflow before Task/VisitorRequest or paid calls."""
    from flask import abort, request
    from models import db, Project
    from services.visitor_access import limits, error
    from services.ai_providers.copilot_limits import execution_reason
    reason = execution_reason('text')
    if reason:
        messages = {'preview': '当前为预览模式，模型执行关闭。', 'admin_paused': '生成已暂停，免费操作仍可用。',
                    'budget_exhausted': '生成额度不足，已有作品仍可使用。'}
        return None, error('EXECUTION_BLOCKED', messages.get(reason, '当前暂不能生成，请检查运行授权和额度。'), 403)
    project = db.session.get(Project, project_id)
    if project is None:
        abort(404)
    if project.creation_type in ('outline', 'descriptions') and not request.path.endswith('/generate/from-description'):
        body = {}  # These routes read their saved source, not body overrides.
    try:
        from services.generation_budget import plan_generation
        return plan_generation(project, body), None
    except PagePlanningError as exc:
        return None, error('PAGE_LIMIT', str(exc), 400)


def instruction(context):
    plan = getattr(context, 'page_plan', None)
    if not plan:
        return ''
    target = plan['planned_page_count']
    origin = '用户明确指定页数' if plan['page_count_source'] == 'user_explicit' else '用户没有指定页数'
    return (f'\n页数约束：{origin}，本次请规划为{target}页（含封面、目录和结束页）。'
            f'单任务硬上限为{plan["max_pages"]}页。只调整内容分配，不凭空新增业务内容。\n')


def check_result(count, plan, partial=False):
    if plan and (count > plan['resolved_pages'] if partial else count != plan['resolved_pages']):
        raise PagePlanningError(f'生成结果与本次计划{plan["resolved_pages"]}页不一致，未完整保存；不自动追加模型请求。')


def completion_message(plan):
    if not plan or plan.get('page_limit_reason') == 'none':
        return None
    target, actual = plan['target_pages'], plan['resolved_pages']
    start = f'你请求生成{target}页' if plan['requested_pages'] is not None else f'默认计划生成{target}页'
    reason = {'max_pages': f'当前单任务最多支持{plan["max_pages"]}页',
              'budget': '受当前可用额度限制',
              'max_pages_and_budget': '结合单任务上限和当前可用额度'}[plan['page_limit_reason']]
    return f'{start}，{reason}，本次已完成{actual}页。'
