# SPDX-License-Identifier: AGPL-3.0-only
"""Propose image sequences from DINOv2 retrieval and verified SIFT overlap."""
from functools import lru_cache
import os
from pathlib import Path
import re

import cv2
import numpy as np
from PIL import Image, ImageOps

MODEL = 'facebook/dinov2-small'


def road_crop(path):
    with Image.open(path) as original:
        image = ImageOps.exif_transpose(original).convert('RGB')
    w, h = image.size
    return image.crop((int(.12*w), int(.12*h), max(int(.98*w),1), max(int(.65*h),1)))


def embeddings(paths, progress):
    import torch
    torch.set_num_threads(max(1, min(4, os.cpu_count() or 1)))
    progress(8, 'Loading DINOv2 (first use may download the model)')
    # A locally cached Meta checkout can also supply the same small model.
    local = os.getenv('SEQUENCE_REVIEW_DINO_REPO')
    if local:
        os.environ.setdefault('XFORMERS_DISABLED', '1')
        torch.hub.set_dir(str(Path(local).resolve().parent))
        model = torch.hub.load(local, 'dinov2_vits14', source='local').eval()
    else:
        from transformers import AutoModel
        model = AutoModel.from_pretrained(MODEL, use_safetensors=True).eval()
    device = 'cuda' if torch.cuda.is_available() else 'cpu'
    model = model.to(device)
    mean = np.array([.485,.456,.406],dtype=np.float32)
    std = np.array([.229,.224,.225],dtype=np.float32)
    output = []
    with torch.inference_mode():
        for start in range(0,len(paths),12):
            batch = paths[start:start+12]
            pixels = np.stack([((np.asarray(road_crop(path).resize((224,224),Image.Resampling.BICUBIC),
                                             dtype=np.float32)/255 - mean)/std).transpose(2,0,1)
                               for path in batch])
            tensor = torch.from_numpy(pixels).to(device)
            features = model(tensor) if local else model(pixel_values=tensor).last_hidden_state[:,0]
            output.append(torch.nn.functional.normalize(features.float(),dim=1).cpu().numpy())
            progress(10+int(45*min(start+len(batch),len(paths))/len(paths)),
                     f'Computed visual features for {start+len(batch)} of {len(paths)} images')
    return np.concatenate(output)


class Geometry:
    def __init__(self, paths):
        self.paths = paths
        self.detector = cv2.SIFT_create(nfeatures=1200)
        self.matcher = cv2.BFMatcher(cv2.NORM_L2)
        self.features = lru_cache(maxsize=100)(self._features)
        cv2.setNumThreads(2)

    def _features(self, index):
        gray = np.asarray(ImageOps.contain(road_crop(self.paths[index]).convert('L'),(840,600)))
        points, descriptors = self.detector.detectAndCompute(gray,None)
        return np.float32([point.pt for point in points]),descriptors,gray.size

    def compare(self, a, b):
        pa,da,sa = self.features(a)
        pb,db,sb = self.features(b)
        if da is None or db is None or min(len(da),len(db))<8:
            return 0,0.,0.
        def ratio_matches(x,y):
            return {(m.queryIdx,m.trainIdx) for row in self.matcher.knnMatch(x,y,k=2)
                    if len(row)==2 for m,n in [row] if m.distance<.75*n.distance}
        forward = ratio_matches(da,db)
        mutual = sorted(forward & {(j,i) for i,j in ratio_matches(db,da)})
        if len(mutual)<8:
            return 0,0.,0.
        ia,ib = np.array(mutual).T
        left,right = pa[ia],pb[ib]
        _,mask = cv2.findHomography(left,right,cv2.RANSAC,4.)
        if mask is None:
            return 0,0.,0.
        mask = mask.ravel().astype(bool)
        count = int(mask.sum())
        coverage = min(cv2.contourArea(cv2.convexHull(left[mask]))/sa,
                       cv2.contourArea(cv2.convexHull(right[mask]))/sb) if count>=4 else 0.
        return count,count/len(mutual),coverage


def numeric_candidates(paths):
    families = {}
    for index,path in enumerate(paths):
        name = re.split(r'\.rf\.',path.stem,1,flags=re.I)[0]
        gopro = re.match(r'^G(\d{3})(\d{4})(?:\D|$)',name,re.I)
        generic = re.search(r'\d+(?=\D*$)',name)
        if gopro:
            family = (path.parent,'gopro',gopro[1])
            number = int(gopro[2])
        elif generic:
            family = (path.parent,name[:generic.start()].casefold(),name[generic.end():].casefold())
            number = int(generic[0])
        else:
            continue
        families.setdefault(family,[]).append((number,index))
    candidates = set()
    for members in families.values():
        ordered = sorted(members)
        for i,(number,a) in enumerate(ordered):
            for later,b in ordered[i+1:i+5]:
                if 0<later-number<=12:
                    candidates.add(tuple(sorted((a,b))))
    return candidates


def propose_sequences(paths, use_numbers, progress):
    if len(paths)<2:
        return [[0]] if paths else []
    vectors = embeddings(paths,progress)
    near = numeric_candidates(paths) if use_numbers else set()
    candidates = set(near)
    neighbors = min(len(paths),13)
    for start in range(0,len(paths),128):
        similarities = vectors[start:start+128] @ vectors.T
        for index,row in enumerate(similarities,start):
            for other in np.argpartition(row,-neighbors)[-neighbors:]:
                if index!=other and row[other]>=.68:
                    candidates.add(tuple(sorted((index,int(other)))))
    matcher = Geometry(paths)
    edges = []
    for checked,(a,b) in enumerate(sorted(candidates),1):
        similarity = float(vectors[a]@vectors[b])
        if similarity >= (.65 if (a,b) in near else .72):
            count,ratio,coverage = matcher.compare(a,b)
            strong = count>=25 and ratio>=.45 and coverage>=.05
            nearby = (a,b) in near and similarity>=.78 and count>=14 and ratio>=.45 and coverage>=.028
            if strong or nearby:
                edges.append((strong,count,coverage,similarity,a,b))
        if checked%20==0 or checked==len(candidates):
            progress(55+int(38*checked/len(candidates)),f'Checked {checked} of {len(candidates)} candidate matches')
    groups = {i:{i} for i in range(len(paths))}
    for strong,count,coverage,similarity,a,b in sorted(edges,reverse=True):
        left,right = groups[a],groups[b]
        if left is right:
            continue
        # Avoid chaining different-looking streets through a shared landmark.
        overlap = vectors[sorted(left)] @ vectors[sorted(right)].T
        if np.quantile(overlap,.10)<.48:
            continue
        joined = left|right
        for index in joined:
            groups[index] = joined
    progress(95,'Preparing review sequences')
    return sorted({tuple(sorted(group)) for group in groups.values()},key=lambda group:group[0])
