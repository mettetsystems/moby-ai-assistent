"""Loss-conscious compaction of old successful tool output; failures stay intact."""
import json
from mobyai.models.inference import ChatMessage


def compact_history(messages, *, retain_pairs=2):
    result = list(messages)
    # The core objective, acceptance criteria and context are separate and untouched.
    for index in range(0, max(0,len(result)-retain_pairs*2),2):
        if index+1 >= len(result):
            break
        try:
            action = json.loads(result[index].content)
            text = result[index+1].content
            start=text.index('[\n')
            outcomes,end=json.JSONDecoder().raw_decode(text[start:])
            if not outcomes or not all(item.get('ok') is True for item in outcomes):
                continue
            # Preserve result metadata, command results and failures; only old read
            # bodies / replaced write payloads are redundant with current files.
            for item in outcomes:
                if item.get('name') in {'read_file','list_directory','search_code','glob','find_files',
                                        'file_info','inspect_python_environment','inspect_node_project'}:
                    item['output']='[Old successful retrieval omitted. Reread source_path/range before relying on it.]'
            for call in action.get('tool_calls',[]):
                if call.get('name') in {'write_file','apply_patch','edit_file'}:
                    for field in ('contents','diff','patch','old_text','new_text'):
                        if field in call.get('arguments',{}):
                            call['arguments'][field]='[Historical applied payload omitted; inspect current files. Do not replay this placeholder.]'
            result[index]=ChatMessage(role='assistant',content=json.dumps(action))
            result[index+1]=ChatMessage(role='user',content=text[:start]+json.dumps(outcomes,indent=2)+text[start+end:])
        except (ValueError,TypeError,KeyError,AttributeError):
            continue
    return result


def invalidate_stale_reads(messages):
    """After a mutation attempt, old file bodies are historical, even if it failed."""
    result = list(messages)
    for index in range(1, len(result), 2):
        try:
            text = result[index].content
            start = text.index('[\n')
            outcomes, end = json.JSONDecoder().raw_decode(text[start:])
            for item in outcomes:
                if item.get('ok') and item.get('name') in {'read_file','file_info',
                        'inspect_python_environment','inspect_node_project','search_code','glob','find_files','list_directory'}:
                    item['output'] = ('[Historical inspection invalidated after a mutation attempt. '
                                      'Any retained hash is historical. Read current state before the next edit.]')
            result[index] = ChatMessage(role='user', content=text[:start] +
                json.dumps(outcomes, indent=2) + text[start+end:])
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
    return result
