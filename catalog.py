# SPDX-License-Identifier: AGPL-3.0-only
"""Folder import, persisted review state, and image/label export."""
from collections import defaultdict
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
from shutil import copy2, rmtree
from uuid import uuid4

EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.tif', '.tiff'}
SEQUENCE = re.compile(r'(?:seq(?:uence)?)[ _-]?(\d+)$', re.I)


def natural(value):
    return tuple((1, int(part)) if part.isdigit() else (0, part.casefold())
                 for part in re.split(r'(\d+)', str(value)))


def within(root, relative):
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError('Choose a path inside the mounted data folder.')
    return path


def read(path, default=None):
    if path.is_file():
        return json.loads(path.read_text(encoding='utf-8-sig'))
    return default


def write(path, value):
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, separators=(',', ':')) + '\n', encoding='utf-8')
    os.replace(tmp, path)


def image_files(folder):
    return sorted((p for p in folder.rglob('*') if p.suffix.lower() in EXTENSIONS
                   and not p.is_symlink() and p.is_file() and p.resolve().is_relative_to(folder)),
                  key=lambda p: natural(p.relative_to(folder).as_posix()))


def label_for(image, folder):
    candidates = [image.with_suffix('.txt')]
    relative = image.relative_to(folder)
    parts = list(relative.parts)
    for i, part in enumerate(parts[:-1]):
        if part.casefold() == 'images':
            candidates.insert(0, folder.joinpath(*parts[:i], 'labels', *parts[i+1:]).with_suffix('.txt'))
    return next((p for p in candidates if p.is_file() and p.resolve().is_relative_to(folder)), None)


def sequence_folder(relative):
    directories = relative.parts[:-1]
    for index in range(len(directories)-1, -1, -1):
        if SEQUENCE.fullmatch(directories[index]):
            return '/'.join(directories[:index+1])
    meaningful = [part for part in directories if part.casefold() != 'images']
    return '/'.join(meaningful) or '(root)'


def sequence_id(number):
    return f'sequence_{number:04d}'


class SeparationConflict(ValueError):
    def __init__(self, token, rules):
        super().__init__('The edit conflicts with an existing street separation.')
        self.token = token
        self.reasons = sorted({rule['reason'] for rule in rules})


class Catalog:
    def __init__(self, root, state_dir, key, document):
        self.root, self.state_dir, self.key, self.document = root, state_dir, key, document
        self.path = state_dir / (key + '.json')

    def import_separations(self):
        if 'cannot_link' in self.document:
            return
        doc = deepcopy(self.document)
        doc['cannot_link'] = []
        source = within(self.root, doc['folder'])
        manifest = read(source/'grouping_manifest.json', {}) if doc['mode']=='open' else {}
        if isinstance(manifest, dict) and {'records','pairs'} <= manifest.keys():
            images = {frame['path']:key for key,frame in doc['frames'].items()}
            representatives = {}
            sequence_groups = {}
            for pair in manifest['pairs']:
                record = manifest['records'][pair['record']]
                image = within(source, pair['output_image'].replace('\\','/'))
                key = images.get(image.relative_to(self.root).as_posix())
                original_id = record.get('original_id')
                if key and original_id and (original_id not in representatives or record.get('filename')==image.name):
                    representatives[original_id] = key
                if record.get('keep_together_group'):
                    sequence_groups[record['sequence_id']] = record['keep_together_group']
            for sequence in doc['sequences'].values():
                sequence['group'] = sequence_groups.get(sequence['group'],sequence['group'])
            for rule in manifest.get('cannot_link',[]):
                left = sorted({representatives[i] for i in rule['left'] if i in representatives})
                right = sorted({representatives[i] for i in rule['right'] if i in representatives})
                if left and right:
                    doc['cannot_link'].append({'left':left,'right':right,'reason':rule['reason']})
        self.commit(doc)

    @classmethod
    def open(cls, root, state_dir, folder, mode, numeric, progress, *, inference='auto'):
        source = within(root, folder)
        if not source.is_dir():
            raise ValueError('Choose a folder that exists.')
        relative = source.relative_to(root).as_posix()
        key = hashlib.sha256(f'{relative}\0{mode}\0{numeric}'.encode()).hexdigest()[:24]
        existing = read(state_dir / (key + '.json'))
        if existing is not None:
            catalog = cls(root, state_dir, key, existing)
            catalog.import_separations()
            return catalog

        progress(5, 'Reading image and label paths')
        groups = defaultdict(list)
        frames = {}
        manifest = read(source / 'grouping_manifest.json') if mode == 'open' else None
        if isinstance(manifest, dict) and {'records', 'pairs'} <= manifest.keys():
            entries = []
            for pair in manifest['pairs']:
                record = manifest['records'][pair['record']]
                label = pair.get('output_label')
                entries.append((within(source, pair['output_image'].replace('\\','/')),
                                within(source, label.replace('\\','/')) if label else None,
                                str(record['sequence_id'])))
        else:
            entries = [(image, label_for(image, source), sequence_folder(image.relative_to(source)))
                       for image in image_files(source)]
        for image, label, group in entries:
            if image.suffix.lower() not in EXTENSIONS or not image.is_file():
                raise ValueError(f'Missing or unsupported image: {image.name}')
            relative_image = image.relative_to(root).as_posix()
            frame_id = hashlib.sha256(relative_image.encode()).hexdigest()[:24]
            if frame_id in frames:
                raise ValueError(f'Image listed more than once: {image.name}')
            original = re.split(r'\.rf\.', image.stem, 1, flags=re.I)[0]
            original = re.sub(r'_JPG$', '', original, flags=re.I)
            frames[frame_id] = {'id': frame_id, 'name': image.name, 'original': original,
                                'path': relative_image,
                                'label': label.relative_to(root).as_posix() if label and label.is_file() else None}
            groups[group].append(frame_id)
        if not frames:
            raise ValueError('This folder has no supported images.')

        if mode == 'sequence':
            from sequencing import propose_sequences
            ids = list(frames)
            paths = [within(root, frames[i]['path']) for i in ids]
            components = propose_sequences(paths, numeric, progress, inference=inference)
            groups = {sequence_id(i): [ids[j] for j in component]
                      for i, component in enumerate(components, 1)}

        reserved = {int(match[1]) for group in groups if (match := SEQUENCE.fullmatch(Path(group).name))}
        next_id = max(reserved, default=0) + 1
        sequences = {}
        for group, members in sorted(groups.items(), key=lambda item: natural(item[0])):
            match = SEQUENCE.fullmatch(Path(group).name)
            sid = sequence_id(int(match[1])) if match else sequence_id(next_id)
            if not match or sid in sequences:
                sid = sequence_id(next_id)
                next_id += 1
            sequences[sid] = {'group': group, 'frames': sorted(members, key=lambda i: natural(frames[i]['name']))}
        document = {'folder': relative, 'mode': mode, 'numeric': numeric, 'frames': frames,
                    'sequences': sequences, 'aliases': {}, 'revision': 0,
                    'flags': {'images': {}, 'sequences': {}}, 'next_id': next_id}
        catalog = cls(root, state_dir, key, document)
        catalog.import_separations()
        return catalog

    def commit(self, document):
        write(self.path, document)
        self.document = document

    def data(self):
        doc = self.document
        sequences = []
        for sid, sequence in sorted(doc['sequences'].items(), key=lambda item: natural(item[0])):
            frames = [{field: doc['frames'][i][field] for field in ('id', 'name', 'original')}
                      for i in sequence['frames']]
            sequences.append({'id': sid, 'group': sequence['group'], 'frames': frames, 'members': len(frames)})
        return {'dataset': doc['folder'], 'revision': doc['revision'], 'sequence_numbering_version': self.key,
                'sequence_aliases': doc['aliases'], 'sequences': sequences}

    def edit(self, payload, progress):
        progress(20, 'Checking the selected frames')
        doc = deepcopy(self.document)
        sequences = doc['sequences']
        source, target = payload.get('source'), payload.get('destination')
        images = payload.get('images')
        kind = payload.get('type')
        if kind not in ('join', 'move') or not isinstance(source, str) or source not in sequences:
            raise ValueError('Choose a current sequence.')
        if not isinstance(images, list) or not images or not all(isinstance(i, str) for i in images):
            raise ValueError('Select at least one frame.')
        selected, current = set(images), set(sequences[source]['frames'])
        if not selected <= current or (kind == 'join' and selected != current):
            raise ValueError('The source changed. Refresh the reviewer and try again.')
        if kind == 'move' and target == 'new':
            target = sequence_id(doc['next_id'])
            doc['next_id'] += 1
            sequences[target] = {'group': sequences[source]['group'], 'frames': []}
        elif not isinstance(target, str) or target == source or target not in sequences:
            raise ValueError('Choose a different destination sequence.')
        before = {i:sequence['group'] for sequence in self.document['sequences'].values() for i in sequence['frames']}
        after = {**before, **dict.fromkeys(selected,sequences[target]['group'])}
        conflicts, preserved = [], []
        for rule in doc['cannot_link']:
            partitions = defaultdict(list)
            for left in rule['left']:
                conflicts_with = tuple(right for right in rule['right']
                                       if before[left]!=before[right] and after[left]==after[right])
                partitions[conflicts_with].append(left)
            for conflicting, left in partitions.items():
                if conflicting:
                    conflicts.append({**rule,'left':left,'right':list(conflicting)})
                right = [i for i in rule['right'] if i not in conflicting]
                if right:
                    preserved.append({**rule,'left':left,'right':right})
        if conflicts:
            token = hashlib.sha256(json.dumps([self.key,doc['revision'],source,target,sorted(selected),conflicts],
                                              sort_keys=True).encode()).hexdigest()
            if payload.get('separation_override')!=token:
                raise SeparationConflict(token,conflicts)
            doc['cannot_link'] = preserved
            doc.setdefault('separation_overrides',[]).append({'source':source,'destination':target,
                                                              'revision':doc['revision']+1,'rules':conflicts})
        for i in selected:
            frame = doc['frames'][i]
            if not within(self.root, frame['path']).is_file() or (frame['label'] and not within(self.root, frame['label']).is_file()):
                raise ValueError(f'Missing image or label for {frame["name"]}.')
        sequences[target]['frames'] = sorted(sequences[target]['frames'] + list(selected),
                                             key=lambda i: (natural(doc['frames'][i]['name']),i))
        sequences[source]['frames'] = [i for i in sequences[source]['frames'] if i not in selected]
        states = doc['flags']['sequences']
        for sid in (source, target):
            states.setdefault(sid, {})['reviewed'] = False
        if not sequences[source]['frames']:
            del sequences[source]
            doc['aliases'] = {sid: target if to == source else to for sid, to in doc['aliases'].items()}
            doc['aliases'][source] = target
            old_note = states.get(source, {}).get('note', '')
            if old_note:
                notes = [note for note in (states[target].get('note'), old_note) if note]
                states[target]['note'] = '\n'.join(dict.fromkeys(notes))[:4000]
        doc['revision'] += 1
        progress(85, 'Saving sequence membership')
        self.commit(doc)
        return target

    def flag(self, payload):
        doc = deepcopy(self.document)
        flags = doc['flags']
        if payload.get('type') == 'outlier':
            images, value = payload.get('images'), payload.get('outlier')
            if (not isinstance(images, list) or not images or type(value) is not bool
                    or not all(isinstance(i,str) and i in doc['frames'] for i in images)):
                raise ValueError('Invalid frame selection.')
            for i in images:
                flags['images'][i] = {'outlier': value}
        elif payload.get('type') == 'sequence':
            sid = payload.get('sequence')
            if not isinstance(sid,str) or sid not in doc['sequences']:
                raise ValueError('Unknown sequence.')
            target = flags['sequences'].setdefault(sid,{})
            if 'reviewed' in payload:
                if type(payload['reviewed']) is not bool:
                    raise ValueError('Invalid review status.')
                target['reviewed'] = payload['reviewed']
            if 'note' in payload:
                note = payload['note']
                if not isinstance(note,str) or len(note)>4000:
                    raise ValueError('Notes can contain up to 4,000 characters.')
                target['note'] = note
        else:
            raise ValueError('Unknown review update.')
        self.commit(doc)
        return flags

    def export(self, exports, name, progress):
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}', name):
            raise ValueError('Use an export folder name with letters, numbers, dots, underscores or hyphens.')
        target = within(exports, name)
        if target.exists():
            raise ValueError('That export folder already exists. Choose another name.')
        temp = within(exports, '.sequence-review-' + uuid4().hex)
        temp.mkdir()
        doc = self.document
        records, pairs = [], []
        try:
            for sid, sequence in doc['sequences'].items():
                used = set()
                for i in sequence['frames']:
                    frame = doc['frames'][i]
                    original = within(self.root, frame['path'])
                    stem = original.stem
                    suffix = 1
                    while stem.casefold() in used:
                        stem = f'{original.stem}_{suffix}'
                        suffix += 1
                    used.add(stem.casefold())
                    image = Path('images')/sid/(stem+original.suffix)
                    (temp/image).parent.mkdir(parents=True,exist_ok=True)
                    copy2(original,temp/image)
                    label = None
                    if frame['label']:
                        label = Path('labels')/sid/(stem+'.txt')
                        (temp/label).parent.mkdir(parents=True,exist_ok=True)
                        copy2(within(self.root,frame['label']),temp/label)
                    records.append({'sequence_id':sid,'frame_id':i,'original_path':frame['path']})
                    pairs.append({'output_image':image.as_posix(),'output_label':label.as_posix() if label else None,
                                  'record':len(records)-1})
                    progress(int(98*len(records)/len(doc['frames'])), f'Exported {len(records)} of {len(doc["frames"])} images')
            write(temp/'sequence_review_export.json',{'records':records,'pairs':pairs,'flags':doc['flags']})
            temp.rename(target)
        except Exception:
            rmtree(temp)
            raise
        return name
