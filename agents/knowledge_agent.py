from langchain_community.vectorstores import Chroma
from langchain_community.embeddings import OpenAIEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_community.document_loaders import UnstructuredExcelLoader
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.documents import Document
from utils.logger import logger
from utils.hash_embeddings import HashEmbeddings
import app_config.settings as settings
import os
import json
import re
import hashlib
from pathlib import Path

class KnowledgeAgent:
    def __init__(self, api_key=None, base_url=None, model_name=None):
        """
        初始化知识智能体
        :param api_key: LLM API Key (如果为空，尝试从配置文件或环境变量获取)
        :param base_url: LLM Base URL
        :param model_name: LLM Model Name
        """
        self.api_key = api_key or settings.LLM_API_KEY
        self.base_url = base_url or settings.LLM_BASE_URL
        self.model_name = model_name or settings.LLM_MODEL_NAME
        self.remote_llm_enabled = bool(getattr(settings, "LLM_REMOTE_ENABLED", True))
        
        # 初始化向量数据库路径
        self.persist_directory = "./data/chroma_db"
        self.local_knowledge_path = "./data/local_knowledge_chunks.json"
        self.local_entries = []
        self.local_chunks = []
        self._load_local_knowledge()
        
        # 初始化 Embedding 模型
        # 注意：DeepSeek 并不直接提供 Embedding 模型，或者使用其 LLM 做 Embedding 比较贵
        # 建议：
        # 1. 使用 OpenAI 的 text-embedding-ada-002 (需要 OpenAI Key)
        # 2. 使用 HuggingFace 本地模型 (免费，但需要下载)
        # 3. 如果 DeepSeek 兼容 Embedding 接口，也可以尝试
        
        # 这里为了简化，假设 DeepSeek 或兼容接口支持 Embedding，
        # 或者我们暂时使用一个本地的轻量级 Embedding 模型以免报错
        # 修正：DeepSeek 官方 API 目前主要提供 Chat 补全。
        # 如果直接用 DeepSeek 的 BaseURL 去调 Embedding 可能会失败。
        # 稳妥起见，如果没有 OpenAI Key，建议使用 HuggingFaceEmbeddings
        # 但为了不引入太多新依赖，我们先尝试用配置的 API 初始化，如果失败则降级。
        
        self.has_llm = False
        self.llm = None
        self.embeddings = None
        try:
            if (not self.remote_llm_enabled):
                logger.info("LLM_REMOTE_ENABLED=false：已禁用远端 LLM，知识问答将使用本地检索/规则兜底")
            elif self.api_key:
                from langchain_openai import ChatOpenAI
                self.llm = ChatOpenAI(
                    model_name=self.model_name,
                    openai_api_key=self.api_key,
                    openai_api_base=self.base_url,
                    temperature=0.5,
                    max_retries=1,
                    request_timeout=settings.LLM_REQUEST_TIMEOUT_SECONDS,
                    max_tokens=settings.LLM_MAX_TOKENS,
                )
                self.has_llm = True
            else:
                logger.warning("未配置 LLM API Key，远端 LLM 不可用；将仅使用本地知识检索")
        except Exception as e:
            logger.error(f"LLM 初始化失败: {e}")
            self.has_llm = False
            self.llm = None

        # Embedding 与向量检索独立于远端 LLM，确保本地模式依然可用。
        try:
            self.embeddings = self._build_embeddings()
        except Exception as e:
            self.embeddings = None
            logger.warning(f"Embedding 初始化失败，知识检索将回退关键词模式: {e}")

        self.vector_store = None
        self._last_query_error_at = 0.0
        self._error_cooldown_seconds = 20
        if self.embeddings:
            self.load_vector_store()

    def _build_hf_embeddings(self, local_files_only=True):
        try:
            from langchain_huggingface import HuggingFaceEmbeddings
        except Exception:
            from langchain_community.embeddings import HuggingFaceEmbeddings

        if local_files_only:
            model_name = str(settings.EMBEDDING_MODEL_NAME or "").strip()
            # local-only 模式下仅接受本地模型目录，避免 HuggingFace 仓库名触发联网重试导致冷启动变慢。
            if model_name and not Path(model_name).exists():
                raise RuntimeError(f"local_model_path_not_found:{model_name}")

        model_kwargs = {"local_files_only": bool(local_files_only)}
        kwargs = {
            "model_name": settings.EMBEDDING_MODEL_NAME,
            "model_kwargs": model_kwargs,
        }
        if settings.EMBEDDING_CACHE_DIR:
            kwargs["cache_folder"] = settings.EMBEDDING_CACHE_DIR

        # 某些版本在 local_files_only=True 时仍会触发 Hub 探测，强制离线变量防止冷启动卡死。
        env_keys = ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE")
        prev_env = {k: os.environ.get(k) for k in env_keys}
        try:
            if local_files_only:
                os.environ["HF_HUB_OFFLINE"] = "1"
                os.environ["TRANSFORMERS_OFFLINE"] = "1"
            else:
                for k in env_keys:
                    os.environ.pop(k, None)
            return HuggingFaceEmbeddings(**kwargs)
        finally:
            for k, v in prev_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def _build_embeddings(self):
        """
        Embedding 初始化策略：
        1) DeepSeek 场景默认本地模型优先（local_files_only），避免网络不通时长时间卡住启动。
        2) 若开启 EMBEDDING_ENABLE_ONLINE_FALLBACK，再尝试在线下载。
        3) 全失败时降级 HashEmbeddings，保证主链路可用。
        """
        if not bool(getattr(settings, "LLM_REMOTE_ENABLED", True)):
            try:
                emb = self._build_hf_embeddings(local_files_only=settings.EMBEDDING_LOCAL_FILES_ONLY)
                mode = "local-only" if settings.EMBEDDING_LOCAL_FILES_ONLY else "online-enabled"
                logger.info(f"Embedding 已初始化（local-first, HuggingFace, {mode}）")
                return emb
            except Exception as e:
                logger.warning(f"local-first HuggingFace Embedding 初始化失败: {e}")
                if settings.EMBEDDING_LOCAL_FILES_ONLY and settings.EMBEDDING_ENABLE_ONLINE_FALLBACK:
                    try:
                        os.environ["HF_HUB_ETAG_TIMEOUT"] = str(settings.HF_HUB_ETAG_TIMEOUT_SECONDS)
                        os.environ["HF_HUB_DOWNLOAD_TIMEOUT"] = str(settings.HF_HUB_DOWNLOAD_TIMEOUT_SECONDS)
                        emb = self._build_hf_embeddings(local_files_only=False)
                        logger.info("Embedding 已切换到在线下载模式初始化")
                        return emb
                    except Exception as online_e:
                        logger.warning(f"Embedding 在线回退失败: {online_e}")
                logger.warning("已降级到 HashEmbeddings（无需网络，召回效果弱于语义向量）")
                return HashEmbeddings(dim=384)

        # DeepSeek 常见场景：不走 OpenAI embedding 接口，直接本地模型。
        if "deepseek" in (self.base_url or "").lower():
            try:
                emb = self._build_hf_embeddings(local_files_only=settings.EMBEDDING_LOCAL_FILES_ONLY)
                mode = "local-only" if settings.EMBEDDING_LOCAL_FILES_ONLY else "online-enabled"
                logger.info(f"Embedding 已初始化（HuggingFace, {mode}）")
                return emb
            except Exception as e:
                logger.warning(f"HuggingFace Embedding 初始化失败: {e}")
                if settings.EMBEDDING_LOCAL_FILES_ONLY and settings.EMBEDDING_ENABLE_ONLINE_FALLBACK:
                    try:
                        os.environ["HF_HUB_ETAG_TIMEOUT"] = str(settings.HF_HUB_ETAG_TIMEOUT_SECONDS)
                        os.environ["HF_HUB_DOWNLOAD_TIMEOUT"] = str(settings.HF_HUB_DOWNLOAD_TIMEOUT_SECONDS)
                        emb = self._build_hf_embeddings(local_files_only=False)
                        logger.info("Embedding 已切换到在线下载模式初始化")
                        return emb
                    except Exception as online_e:
                        logger.warning(f"Embedding 在线回退失败: {online_e}")

                logger.warning("已降级到 HashEmbeddings（无需网络，召回效果弱于语义向量）")
                return HashEmbeddings(dim=384)

        # 非 DeepSeek：优先 OpenAI 兼容 embedding。
        try:
            return OpenAIEmbeddings(
                openai_api_key=self.api_key,
                openai_api_base=self.base_url
            )
        except Exception as e:
            logger.warning(f"OpenAI Embedding 初始化失败: {e}")
            try:
                emb = self._build_hf_embeddings(local_files_only=settings.EMBEDDING_LOCAL_FILES_ONLY)
                mode = "local-only" if settings.EMBEDDING_LOCAL_FILES_ONLY else "online-enabled"
                logger.info(f"已回退到 HuggingFace Embedding（{mode}）")
                return emb
            except Exception as inner_e:
                logger.warning(f"HuggingFace Embedding 回退失败: {inner_e}")
                logger.warning("已降级到 HashEmbeddings（无需网络，召回效果弱于语义向量）")
                return HashEmbeddings(dim=384)

    def _load_local_knowledge(self):
        """加载本地知识片段（向量检索不可用时兜底）。"""
        path = Path(self.local_knowledge_path)
        if not path.exists():
            self.local_entries = []
            self.local_chunks = []
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))

            entries = []
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        content = self._normalize_chunk(item.get("content", ""))
                        if content:
                            entries.append({
                                "source": str(item.get("source", "")).strip(),
                                "content": content,
                            })
                    else:
                        content = self._normalize_chunk(str(item))
                        if content:
                            # 兼容旧格式（纯字符串列表）
                            entries.append({"source": "legacy", "content": content})

            self.local_entries = entries
            self.local_chunks = [e["content"] for e in entries]
            if self.local_chunks:
                logger.info(f"已加载本地知识片段 {len(self.local_chunks)} 条")
        except Exception as e:
            logger.warning(f"加载本地知识片段失败: {e}")
            self.local_entries = []
            self.local_chunks = []

    def _save_local_knowledge(self):
        path = Path(self.local_knowledge_path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(self.local_entries, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
        except Exception as e:
            logger.warning(f"保存本地知识片段失败: {e}")

    def _read_text_file(self, file_path):
        """尽量正确解析 txt 编码，避免中文乱码。"""
        raw = Path(file_path).read_bytes()
        encodings = [
            "utf-8-sig",
            "utf-8",
            "utf-16",
            "utf-16le",
            "utf-16be",
            "gb18030",
            "gbk",
            "big5",
        ]

        for enc in encodings:
            try:
                content = raw.decode(enc)
                if content and content.strip():
                    return content
            except Exception:
                continue

        # 最后兜底，尽量保留可读字符
        return raw.decode("utf-8", errors="ignore")

    def _load_documents(self, file_path):
        suffix = Path(file_path).suffix.lower()
        if suffix == ".txt":
            content = self._read_text_file(file_path)
            return [Document(page_content=content, metadata={"source": file_path})]
        if suffix == ".xlsx":
            loader = UnstructuredExcelLoader(file_path)
            return loader.load()
        logger.warning("不支持的文件格式")
        return []

    def _build_splitter(self):
        """中文友好的递归分块，提升向量召回质量。"""
        return RecursiveCharacterTextSplitter(
            chunk_size=settings.RAG_CHUNK_SIZE,
            chunk_overlap=settings.RAG_CHUNK_OVERLAP,
            separators=["\n\n", "\n", "。", "！", "？", "；", "，", " ", ""],
        )

    def _normalize_chunk(self, text):
        text = (text or "").replace("\u3000", " ")
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def _chunk_id(self, source, idx, content):
        raw = f"{source}::{idx}::{content[:120]}"
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    def _tokenize(self, text):
        text = (text or "").lower()
        words = re.findall(r"[a-z0-9]+", text)
        chars = re.findall(r"[\u4e00-\u9fff]", text)
        return set(words + chars)

    def _keyword_retrieve(self, question, k=3):
        """向量检索不可用时，使用关键词匹配做兜底检索。"""
        if not self.local_entries:
            return []

        q = (question or "").strip().lower()
        q_tokens = self._tokenize(q)
        scored = []
        for entry in self.local_entries:
            c = (entry.get("content") or "").strip()
            if not c:
                continue
            c_lower = c.lower()
            c_tokens = self._tokenize(c_lower)
            score = len(q_tokens & c_tokens)
            if q and q in c_lower:
                score += 8
            if score > 0:
                scored.append((score, c))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [item[1] for item in scored[:k]]

    def _vector_retrieve(self, question, k):
        """向量检索（相关度阈值 + MMR 兜底）。"""
        if not self.vector_store:
            return []

        results = []
        try:
            pairs = self.vector_store.similarity_search_with_relevance_scores(
                question,
                k=max(k, settings.RAG_RETRIEVAL_K),
            )
            for doc, score in pairs:
                if not doc or not doc.page_content:
                    continue
                try:
                    score_val = float(score)
                except Exception:
                    score_val = 0.0
                if score_val >= settings.RAG_MIN_RELEVANCE_SCORE:
                    results.append((self._normalize_chunk(doc.page_content), score_val))
        except Exception as e:
            logger.warning(f"向量相关度检索失败: {e}")

        if results:
            results.sort(key=lambda x: x[1], reverse=True)
            return [x[0] for x in results[:k]]

        try:
            docs = self.vector_store.max_marginal_relevance_search(
                question,
                k=k,
                fetch_k=max(settings.RAG_RETRIEVAL_FETCH_K, k * 2),
            )
            return [self._normalize_chunk(d.page_content) for d in docs if d and d.page_content]
        except Exception as e:
            logger.warning(f"向量 MMR 检索失败: {e}")
            return []

    def _retrieve_context(self, question):
        """优先向量检索，必要时混入关键词检索。"""
        k = settings.RAG_RETRIEVAL_K
        vector_chunks = self._vector_retrieve(question, k=k)
        if vector_chunks:
            # 召回数量不足时混入关键词结果，增强细节命中
            keyword_chunks = self._keyword_retrieve(question, k=max(1, k - len(vector_chunks)))
            merged = []
            for chunk in vector_chunks + keyword_chunks:
                if chunk and chunk not in merged:
                    merged.append(chunk)
            context = "\n".join(merged[:k]).strip()
            if context:
                return context, "vector+keyword" if keyword_chunks else "vector"

        chunks = self._keyword_retrieve(question, k=k)
        if chunks:
            return "\n".join(chunks).strip(), "keyword"

        return "", "none"

    def _fallback_answer_from_context(self, question, context):
        """LLM 不可用时，从上下文抽取最相关句子作为兜底回答。"""
        if not context:
            return None
        candidates = [s.strip() for s in re.split(r"[。！？!?\\n]+", context) if s.strip()]
        if not candidates:
            return None

        q_tokens = self._tokenize(question)
        q_lower = (question or "").lower()
        best = candidates[0]
        best_score = -1
        for sent in candidates:
            score = len(q_tokens & self._tokenize(sent))
            sent_lower = sent.lower()

            # 常见直播问法意图加权
            if any(k in q_lower for k in ["价格", "多少钱", "多少", "几元", "价位", "优惠", "便宜"]):
                if any(k in sent_lower for k in ["元", "只要", "原价", "特价", "优惠", "减", "买一送", "下单"]):
                    score += 6
                if re.search(r"(¥|￥)\s*\d+|\d+\s*元", sent_lower):
                    score += 4
                elif re.search(r"\d{2,}", sent_lower) and any(k in sent_lower for k in ["价", "只要", "减"]):
                    score += 3
            if any(k in q_lower for k in ["材质", "材料", "什么做的"]):
                if any(k in sent_lower for k in ["材质", "高温丝", "真人发", "发丝"]):
                    score += 6
            if any(k in q_lower for k in ["发货", "多久到", "几天到", "物流"]):
                if any(k in sent_lower for k in ["发货", "下单", "48小时", "快递"]):
                    score += 6

            if score > best_score:
                best_score = score
                best = sent
        return f"{best}。"

    def load_vector_store(self):
        """加载或创建向量数据库"""
        if not os.path.exists(self.persist_directory):
            os.makedirs(self.persist_directory)
            
        try:
            self.vector_store = Chroma(
                persist_directory=self.persist_directory, 
                embedding_function=self.embeddings
            )
            logger.info("向量数据库加载成功")
        except Exception as e:
            logger.error(f"向量数据库加载失败: {e}")

    def ingest_knowledge(self, file_path):
        """导入知识库 (Text, Excel)"""
        logger.info(f"正在导入知识: {file_path}")
        try:
            documents = self._load_documents(file_path)
            if not documents:
                return

            source = str(Path(file_path).resolve())
            for d in documents:
                if not isinstance(d.metadata, dict):
                    d.metadata = {}
                d.metadata["source"] = source

            splitter = self._build_splitter()
            split_docs = splitter.split_documents(documents)

            # 清洗与去重分块
            chunks = []
            metadatas = []
            for idx, doc in enumerate(split_docs):
                content = self._normalize_chunk(doc.page_content)
                if not content:
                    continue
                chunks.append(content)
                meta = dict(doc.metadata or {})
                meta["source"] = source
                meta["chunk_index"] = idx
                metadatas.append(meta)

            if not chunks:
                logger.warning("导入的知识为空，跳过")
                return

            # 本地兜底知识片段（保证向量库不可用时仍可检索）
            # 同源覆盖更新，避免重复导入导致语料污染。
            self.local_entries = [
                e for e in self.local_entries
                if e.get("source") not in {source, "legacy"}
            ]
            seen = {e.get("content") for e in self.local_entries}
            for c in chunks:
                if c in seen:
                    continue
                self.local_entries.append({"source": source, "content": c})
                seen.add(c)
            self.local_chunks = [e["content"] for e in self.local_entries]
            self._save_local_knowledge()

            if self.vector_store:
                # 同源文件更新时，先删除旧向量，避免历史脏数据污染召回。
                try:
                    self.vector_store.delete(where={"source": source})
                except Exception:
                    pass

                ids = [self._chunk_id(source, i, c) for i, c in enumerate(chunks)]
                self.vector_store.add_texts(chunks, metadatas=metadatas, ids=ids)
                self.vector_store.persist()
                logger.info(f"成功导入 {len(chunks)} 条知识片段（vector+local）")
            else:
                logger.warning(f"向量数据库不可用，已仅保存本地知识片段 {len(chunks)} 条")
            
        except Exception as e:
            logger.error(f"导入知识库失败: {e}")

    def _language_instruction(self, language):
        lang_map = {
            "zh-CN": "最终输出必须使用简体中文。无论用户问题、知识库内容或语气模板使用什么语言，都只输出简体中文，不要混用英文或西语。",
            "en-US": "Final output must be in English only. Even if the user question, knowledge context, or tone template is in another language, translate and answer in English only.",
            "es-ES": "La salida final debe ser solo en español. Aunque la pregunta, el contexto o la plantilla de tono estén en otro idioma, traduce y responde solo en español.",
        }
        return lang_map.get(language, lang_map["zh-CN"])

    def _tone_rule(self, language, tone_template):
        tone = str(tone_template or "").strip()
        if not tone:
            return ""
        tone = tone[:320]
        target_map = {
            "zh-CN": "简体中文",
            "en-US": "English",
            "es-ES": "Español",
        }
        target = target_map.get(language, "简体中文")
        return (
            "语气模板（可能与目标语言不同）："
            f"<<<{tone}>>>。"
            f"请仅提取其中的风格/语气/节奏并应用；最终输出必须严格使用{target}。"
        )

    def _is_text_in_target_language(self, text, language):
        s = str(text or "").strip()
        if not s:
            return True
        zh_chars = len(re.findall(r"[\u4e00-\u9fff]", s))
        latin_words = re.findall(r"[a-zA-Z]+", s)
        latin_count = len(latin_words)
        low = s.lower()

        if language == "zh-CN":
            return zh_chars >= 2
        if language == "en-US":
            if zh_chars > 0:
                return False
            return latin_count >= 2
        if language == "es-ES":
            if zh_chars > 0:
                return False
            if re.search(r"[áéíóúñ¿¡]", low):
                return True
            es_markers = [
                " el ", " la ", " los ", " las ", " de ", " que ", " para ",
                " envío ", " envio ", " precio ", " talla ", " oferta ", " gracias ",
            ]
            padded = f" {low} "
            return any(m in padded for m in es_markers) or latin_count >= 2
        return True

    def ensure_output_language(self, text, language, tone_template=None, force_tone=False):
        """
        统一输出语言兜底：
        - 若文本已是目标语言，直接返回
        - 若语言不匹配且 LLM 可用，做一次轻量改写/翻译，保持原意和语气
        """
        raw = str(text or "").strip()
        if not raw:
            return raw
        lang_code = language or settings.DEFAULT_REPLY_LANGUAGE
        lang_ok = self._is_text_in_target_language(raw, lang_code)
        if lang_ok and (not force_tone or not str(tone_template or "").strip()):
            return raw
        if not self.has_llm or not self.llm:
            return raw

        try:
            lang_rule = self._language_instruction(lang_code)
            tone_rule = self._tone_rule(lang_code, tone_template)
            if lang_ok:
                rewrite_system = (
                    "你是直播助播文本润色器。"
                    "只做语气风格改写，不扩展事实，不新增信息，不改变核心含义。"
                    f"{lang_rule}"
                    f"{tone_rule}"
                    "输出要求：一句短句，口语化。"
                )
                rewrite_user = f"请按语气模板改写这句话（语言不变）：\n{raw}"
            else:
                rewrite_system = (
                    "你是直播助播文本改写器。只做语言统一，不扩展事实，不新增信息。"
                    f"{lang_rule}"
                    f"{tone_rule}"
                    "输出要求：一句短句，口语化，尽量保留原有风格。"
                )
                rewrite_user = f"请把下面这句话改写为目标语言输出：\n{raw}"
            response = self.llm.invoke([
                SystemMessage(content=rewrite_system),
                HumanMessage(content=rewrite_user),
            ])
            rewritten = str(getattr(response, "content", "") or "").strip()
            if rewritten:
                return rewritten
            return raw
        except Exception as e:
            logger.warning(f"统一输出语言改写失败，返回原文: {e}")
            return raw

    def _no_knowledge_reply(self, language):
        msg_map = {
            "zh-CN": "知识库暂无该信息，我再帮你补充后回答你～",
            "en-US": "The knowledge base does not have this yet. I can add it and get back to you.",
            "es-ES": "La base de conocimiento aún no tiene esta información. Puedo agregarla y responderte luego.",
        }
        return msg_map.get(language, msg_map["zh-CN"])

    def _system_prompt_template(self, language, with_context=True):
        templates = {
            "zh-CN": {
                "with_context": "你是一位热情的直播助播。必须优先根据提供的上下文回答用户问题。禁止编造；如果上下文没有答案，明确回复“知识库暂无该信息”。要求：一句话、口语化、尽量短、带1个表情。",
                "no_context": "你是一位热情的直播助播。直接回答用户问题。要求：一句话、口语化、尽量短、带1个表情。",
            },
            "en-US": {
                "with_context": "You are an energetic live-stream cohost. Answer strictly based on the provided context first. Do not fabricate details. If the context has no answer, clearly say the knowledge base does not have that information. Keep it to one short, spoken-style sentence with one emoji.",
                "no_context": "You are an energetic live-stream cohost. Answer the user directly in one short, spoken-style sentence with one emoji.",
            },
            "es-ES": {
                "with_context": "Eres una coanfitriona de directo con energía. Responde primero según el contexto proporcionado. No inventes datos. Si el contexto no tiene respuesta, indícalo claramente. Responde en una sola frase breve, estilo hablado, con un emoji.",
                "no_context": "Eres una coanfitriona de directo con energía. Responde en una sola frase breve, estilo hablado, con un emoji.",
            },
        }
        block = templates.get(language, templates["zh-CN"])
        return block["with_context"] if with_context else block["no_context"]

    def query(self, question, language=None, tone_template=None):
        """
        基于 RAG 回答问题
        """
        import time
        lang_code = language or settings.DEFAULT_REPLY_LANGUAGE

        if not self.has_llm or not self.llm:
            context, _ = self._retrieve_context(question)
            fallback = self._fallback_answer_from_context(question, context)
            if fallback:
                return self.ensure_output_language(fallback, lang_code, tone_template=tone_template)
            if self.local_chunks:
                return self._no_knowledge_reply(lang_code)
            return None
        if self._last_query_error_at and time.time() - self._last_query_error_at < self._error_cooldown_seconds:
            context, _ = self._retrieve_context(question)
            fallback = self._fallback_answer_from_context(question, context)
            if fallback:
                return fallback
            if self.local_chunks:
                return self._no_knowledge_reply(lang_code)
            return None

        context = ""
        try:
            context, context_source = self._retrieve_context(question)
            logger.debug(f"知识检索来源: {context_source}, 命中上下文长度: {len(context)}")

            lang_rule = self._language_instruction(lang_code)
            tone_rule = self._tone_rule(lang_code, tone_template)

            if context:
                system_prompt = (
                    f"{self._system_prompt_template(lang_code, with_context=True)}"
                    f"{lang_rule}"
                    "上下文可能是中英混合，请先理解再用目标语言回答，不要照抄原文语言。"
                    f"{tone_rule}"
                )
                if lang_code == "en-US":
                    user_prompt = f"Context: {context}\n\nUser question: {question}"
                elif lang_code == "es-ES":
                    user_prompt = f"Contexto: {context}\n\nPregunta del usuario: {question}"
                else:
                    user_prompt = f"上下文：{context}\n\n用户问题：{question}"
            elif self.local_chunks:
                # 已有知识库但本次未检索到，明确告知，避免通用胡答
                return self._no_knowledge_reply(lang_code)
            else:
                system_prompt = (
                    f"{self._system_prompt_template(lang_code, with_context=False)}"
                    f"{lang_rule}"
                    f"{tone_rule}"
                )
                user_prompt = question

            response = self.llm.invoke([
                SystemMessage(content=system_prompt),
                HumanMessage(content=user_prompt)
            ])
            
            return self.ensure_output_language(response.content, lang_code, tone_template=tone_template)
            
        except Exception as e:
            self._last_query_error_at = time.time()
            logger.error(f"RAG 查询失败: {e}")
            fallback = self._fallback_answer_from_context(question, context)
            if fallback:
                logger.warning("LLM 调用失败，已回退到基于知识库的规则回答")
                return self.ensure_output_language(fallback, lang_code, tone_template=tone_template)
            if self.local_chunks:
                return self._no_knowledge_reply(lang_code)
            return None

if __name__ == "__main__":
    # 测试代码
    agent = KnowledgeAgent()
    # agent.ingest_knowledge("data/products.txt")
    # print(agent.query("这件衣服有红色的吗？"))
