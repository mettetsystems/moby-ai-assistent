"""Canonical argument shapes shared by tool validation and model documentation.

Legacy aliases remain accepted. Tool-specific semantic checks still run in tools.py.
"""
import json
from pydantic import BaseModel, ConfigDict, StrictInt, StrictStr


class Arguments(BaseModel):
    model_config = ConfigDict(extra='allow')


class PathArguments(Arguments):
    path: StrictStr


class ReadArguments(PathArguments):
    start_line: StrictInt | None = None
    end_line: StrictInt | None = None
    max_lines: StrictInt | None = None
    offset: StrictInt | None = None
    limit: StrictInt | None = None


class WriteArguments(PathArguments):
    contents: StrictStr


class PatchArguments(Arguments):
    diff: StrictStr | None = None
    patch: StrictStr | None = None


class CommandArguments(Arguments):
    command: list[StrictStr] | StrictStr


class RunArguments(CommandArguments):
    cwd: StrictStr | None = None


class DirectoryArguments(Arguments):
    path: StrictStr | None = None


class SearchArguments(DirectoryArguments):
    pattern: StrictStr
    max_hits: StrictInt | None = None


SCHEMAS = {'read_file':ReadArguments, 'write_file':WriteArguments,
           'apply_patch':PatchArguments,'run_test':CommandArguments,'run_command':RunArguments,
           'list_directory':DirectoryArguments,'search_code':SearchArguments,
           'glob':SearchArguments,'find_files':SearchArguments}


def tool_guidance():
    rows=[]
    for name,model in SCHEMAS.items():
        schema=model.model_json_schema()
        rows.append({'name':name,'required':schema.get('required',[]),
                     'arguments':schema['properties']})
    return ('Supported tool argument schemas (semantic path/range checks still apply):\n'+
            json.dumps(rows,separators=(',',':'))+
            '\nresearch_lookup accepts query for local evidence. Outbound research_search is not a worker tool.')
