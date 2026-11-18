"""Discover candidate sources without conflating independent same-name files."""

import hashlib
import re
from collections import OrderedDict

from .models import URL

FILE = re.compile(r'[^\s/\\<>"：:，、。；（）\[\]]+\.(?:tar\.gz|tgz|zip|7z|rar|gz|tar|log|txt|trace|perfetto-trace|mp4|png|jpg|jpeg)', re.I)
LOG_ID = re.compile(r'(?<!\w)log_[A-Za-z0-9][A-Za-z0-9._-]*', re.I)
GROUP = re.compile(r'(?:钉钉群|日志群|讨论群|群名)\s*[：:]\s*[“"「]?([^\s，。；“”"「」]+?群)(?=[\s，。；”"」]|$)')
BRACKET_GROUP = re.compile(r'(?:见|来自)\s*[：:]?\s*[【\[「“]([^】\]」”\n]+)[】\]」”]\s*(?:钉钉)?群?', re.U)


def key(value):
    return hashlib.sha256(str(value).encode()).hexdigest()[:16]


def discover(snapshot):
    candidates, gaps, seen = [], [], set()
    texts = ([snapshot.get('description', '')]
             + [c.get('text', '') for c in snapshot.get('comments', [])]
             + [c.get('text', '') for c in snapshot.get('timeline', [])])
    # Older browser captures treated the next line after “日志群：” as a
    # group name even when it was the explicitly written log identifier.
    groups = [group for group in snapshot.get('groups', [])
              if not LOG_ID.fullmatch(group.get('name', ''))]
    for text in texts:
        names = GROUP.findall(text) + BRACKET_GROUP.findall(text)
        for name in names:
            name = name.strip()
            if not any(g.get('name') == name for g in groups):
                groups.append({'name': name})
    mentions = list(snapshot.get('file_mentions', []))
    explicit_names = {m.get('name') for m in mentions}
    if groups:
        for text in texts:
            # URLs are handled separately, so filename discovery does not extract URL path suffixes.
            clean = URL.sub('', text)
            for name in FILE.findall(clean) + LOG_ID.findall(clean):
                if name not in explicit_names:
                    mentions.append({'name': name})
                    explicit_names.add(name)
    for kind in ('attachments', 'images'):
        for index, item in enumerate(snapshot.get(kind, [])):
            identity = str(item.get('id') or key(item.get('url') or item.get('local_path') or (kind + str(index))))
            candidate = dict(item)
            candidate.update(id=kind + '-' + identity,
                             target_id=item.get('target_id') or kind + '-' + identity,
                             priority=3, name=item.get('name', ''),
                             transport=item.get('transport') or ('local' if item.get('local_path') else 'browser'),
                             source={'kind': kind, 'id': identity, 'url': item.get('url', ''),
                                     'comment_ids': item.get('comment_ids', [])})
            if candidate.get('url'):
                seen.add(candidate['url'])
            candidates.append(candidate)
    for index, item in enumerate(mentions):
        group = item.get('group')
        if not group and len(groups) == 1:
            group = groups[0]
        if not group and len(groups) > 1:
            gaps.append('文件线索存在多个关联群，需要明确目标群：' + item.get('name', '未命名'))
            continue
        if not group:
            gaps.append('文件线索缺少关联钉钉群：' + item.get('name', '未命名'))
            continue
        matching = [c for c in candidates if c['priority'] == 3 and c['name'] == item.get('name')]
        target_id = item.get('target_id') or (matching[0]['target_id'] if len(matching) == 1 else 'mention-' + str(index))
        candidates.append(dict(item, id='mention-' + str(index),
                               target_id=target_id,
                               priority=1, transport='dws', group=group,
                               source={'kind': 'group_mention', 'name': item.get('name', '')}))
    for index, group in enumerate(groups):
        related = [candidate for candidate in candidates
                   if candidate['priority'] == 1 and candidate.get('group') == group]
        if related:
            for sub_index, mention in enumerate(related):
                candidates.append({'id': 'group-' + str(index) + '-' + str(sub_index),
                                   'target_id': mention['target_id'], 'priority': 2,
                                   'transport': 'dws', 'name': mention.get('name', ''), 'group': group,
                                   'source': {'kind': 'group_files', 'name': mention.get('name', '')}})
        else:
            candidates.append({'id': 'group-' + str(index), 'target_id': 'group-' + str(index),
                               'priority': 2, 'transport': 'dws', 'name': '', 'group': group,
                               'source': {'kind': 'group_files'}})
    for text in texts:
        for url in URL.findall(text):
            if url in seen:
                continue
            seen.add(url)
            candidates.append({'id': 'url-' + key(url), 'target_id': 'url-' + key(url),
                               'priority': 4, 'transport': 'http', 'url': url, 'name': '',
                               'source': {'kind': 'text_url', 'url': url}})
    return candidates, gaps


def ordered_targets(candidates):
    result = OrderedDict()
    for item in sorted(candidates, key=lambda c: c['priority']):
        result.setdefault(item['target_id'], []).append(item)
    return result


def image_clues(text):
    return {'urls': list(dict.fromkeys(URL.findall(text))),
            'files': list(dict.fromkeys(FILE.findall(text)))}
