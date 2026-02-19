import hashlib
import re
from typing import List


class HashEmbeddings:
    """
    纯本地哈希向量：不依赖 sentence-transformers。
    用于网络受限或依赖缺失时的稳定降级。
    """

    def __init__(self, dim: int = 384):
        self.dim = max(64, int(dim))

    def _tokenize(self, text: str):
        text = (text or "").lower()
        words = re.findall(r"[a-z0-9]+", text)
        chars = re.findall(r"[\u4e00-\u9fff]", text)
        return words + chars

    def _vec(self, text: str) -> List[float]:
        vec = [0.0] * self.dim
        tokens = self._tokenize(text)
        if not tokens:
            return vec

        for token in tokens:
            h = hashlib.md5(token.encode("utf-8")).digest()
            idx = int.from_bytes(h[:4], "big") % self.dim
            sign = 1.0 if (h[4] & 1) == 0 else -1.0
            vec[idx] += sign

        # L2 归一化，减小长度差异影响
        norm = sum(v * v for v in vec) ** 0.5
        if norm > 1e-12:
            vec = [v / norm for v in vec]
        return vec

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> List[float]:
        return self._vec(text)
