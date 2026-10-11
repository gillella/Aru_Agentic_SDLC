"""Pure genuine-session validation; no model calls or publishing."""


def session(events, model):
    if not isinstance(events, list) or any(not isinstance(e, dict) for e in events):
        raise ValueError('Malformed session events')
    initial = [e.get('init') for e in events if e.get('event') == 'init']
    terminal = [e.get('result') for e in events if e.get('event') == 'result']
    if (len(initial) != 1 or not isinstance(initial[0], dict) or initial[0].get('model') != model
            or len(terminal) != 1 or not isinstance(terminal[0], dict)):
        raise ValueError('Missing or contradictory actual session evidence')
    end = terminal[0]
    if end.get('status') != 'SUCCESS' or not isinstance(end.get('conversation_id'), str) or not end['conversation_id']:
        raise ValueError('Incomplete actual session evidence')
    if initial[0].get('conversation_id', end['conversation_id']) != end['conversation_id']:
        raise ValueError('Unbound conversation')
    return end


def steps(events, end):
    finish_index, finish_done, ended = None, False, False
    for event in events:
        if event.get('event') == 'result':
            ended = True
        step = event.get('step_update', {})
        if not isinstance(step, dict) or step.get('step_type') == 'subagent' or step.get('subagent_info'):
            raise ValueError('Unexpected subagent execution')
        if step.get('step_type') != 'tool':
            continue
        info = step.get('tool_info', {})
        if (not isinstance(info, dict) or step.get('tool_name') != 'finish'
                or info.get('name', 'finish') != 'finish' or info.get('error')):
            raise ValueError('Unexpected tool execution')
        index = step.get('step_index')
        if (ended or type(index) is not int or index < 0 or step.get('conversation_id') != end['conversation_id']
                or step.get('state') not in ('ACTIVE', 'DONE')):
            raise ValueError('Unbound finish step')
        if finish_index is not None and (index != finish_index or finish_done):
            raise ValueError('Multiple finish steps')
        finish_index, finish_done = index, step['state'] == 'DONE'

    if finish_index is not None and not finish_done:
        raise ValueError('Unfinished terminal step')


def findings(values, bundle):
    if not isinstance(values, list):
        raise ValueError('Malformed findings')
    for value in values:
        if not isinstance(value, dict) or set(value) != {'path', 'line', 'severity', 'body'}:
            raise ValueError('Malformed finding')
        if value['path'] not in bundle['head_sources']:
            raise ValueError('Unavailable finding source')
        if type(value['line']) is not int or not 1 <= value['line'] <= len(bundle['head_sources'][value['path']].splitlines()):
            raise ValueError('Invalid finding line')
        if (value['severity'] not in ('high', 'medium', 'low')
                or not isinstance(value['body'], str) or not value['body'].strip()):
            raise ValueError('Missing finding evidence')


def answer(result, bundle):
    fields = {'head', 'nonce', 'decision', 'summary', 'reviewed_paths', 'findings', 'limitations', 'resolutions'}
    if not isinstance(result, dict) or set(result) != fields:
        raise ValueError('Malformed review')
    if result['head'] != bundle['head'] or result['nonce'] != bundle['nonce']:
        raise ValueError('Wrong immutable snapshot')
    paths = result['reviewed_paths']
    if (not isinstance(paths, list) or not all(isinstance(p, str) for p in paths)
            or len(paths) != len(set(paths)) or set(paths) != set(bundle['changed_paths'])):
        raise ValueError('Incomplete source coverage')
    if result['decision'] not in ('APPROVE', 'REQUEST_CHANGES', 'INCOMPLETE'):
        raise ValueError('Unknown review decision')
    if not isinstance(result['summary'], str) or not result['summary'].strip():
        raise ValueError('Missing review rationale')
    findings(result['findings'], bundle)
    if not isinstance(result['limitations'], list) or not all(isinstance(s, str) for s in result['limitations']):
        raise ValueError('Malformed limitations')
    if result['decision'] == 'APPROVE' and (result['findings'] or result['limitations']):
        raise ValueError('Contradictory approval')
    if result['decision'] == 'REQUEST_CHANGES' and not result['findings']:
        raise ValueError('Changes requested without evidence')
    dispositions(result, bundle)
    return result


def dispositions(result, bundle):
    resolutions = result['resolutions']
    previous = {r['id'] for r in bundle.get('previous_reviews', [])
                if r.get('login', '').lower() == 'arureviewauthority[bot]'
                and r.get('state') == 'CHANGES_REQUESTED' and type(r.get('id')) is int}
    seen = set()
    if not isinstance(resolutions, list):
        raise ValueError('Malformed dispositions')
    for item in resolutions:
        if (not isinstance(item, dict) or set(item) != {'review_id', 'evidence'}
                or type(item['review_id']) is not int or item['review_id'] not in previous
                or item['review_id'] in seen or not isinstance(item['evidence'], str)
                or not item['evidence'].strip()):
            raise ValueError('Unbound disposition')
        seen.add(item['review_id'])
    if result['decision'] == 'APPROVE' and seen != previous:
        raise ValueError('Prior findings lack verification')


def validate(events, bundle, model):
    end = session(events, model)
    steps(events, end)
    return end, answer(end.get('structured_output'), bundle)
