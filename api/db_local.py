# ==============================================================================
# BANCO DE DADOS LOCAL (SQLite) — autônomo, sem dependências externas.
# Guarda sessões, mensagens e os documentos do RAG (embeddings) num único
# arquivo em ../data/cyborg.db. A busca vetorial do RAG é feita em Python.
# ==============================================================================
import os
import io
import csv
import uuid
import hashlib
import sqlite3
import threading
from datetime import datetime, timezone, timedelta

try:
    import numpy as np
except Exception:
    np = None

DB_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
DB_PATH = os.path.join(DB_DIR, "cyborg.db")

_lock = threading.Lock()
_vec_cache = None  # (ids, conteudos, matriz_normalizada)


def _conn():
    os.makedirs(DB_DIR, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL;")
    c.execute("PRAGMA foreign_keys=ON;")
    return c


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def init_db():
    with _lock, _conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS chat_sessions (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                title TEXT NOT NULL,
                user_name TEXT,
                is_pinned INTEGER DEFAULT 0,
                oculta_para_usuario INTEGER DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS chat_messages (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
                role TEXT NOT NULL CHECK (role IN ('user','assistant')),
                content TEXT NOT NULL,
                used_rag INTEGER DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS documentos (
                id TEXT PRIMARY KEY,
                nome_documento TEXT,
                conteudo TEXT,
                embedding BLOB
            );
            CREATE INDEX IF NOT EXISTS idx_msg_session ON chat_messages(session_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_sess_user ON chat_sessions(user_id);
            CREATE TABLE IF NOT EXISTS config (
                chave TEXT PRIMARY KEY,
                valor TEXT
            );
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                pass_hash TEXT NOT NULL,
                name TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS folders (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_folders_user ON folders(user_id);
            CREATE TABLE IF NOT EXISTS user_prefs (
                user_id TEXT PRIMARY KEY,
                memory_enabled INTEGER DEFAULT 0,
                memory_ready INTEGER DEFAULT 0,
                memory_text TEXT DEFAULT '',
                msgs_since INTEGER DEFAULT 0,
                updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS activity_log (
                id TEXT PRIMARY KEY,
                user_id TEXT,
                tipo TEXT,
                detalhe TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_activity_created ON activity_log(created_at);
            """
        )
        # Valores padrão do painel admin (só inserem se ainda não existirem)
        c.execute("INSERT OR IGNORE INTO config (chave,valor) VALUES ('gravar_no_bd','true')")
        c.execute("INSERT OR IGNORE INTO config (chave,valor) VALUES ('rag_padrao','false')")
        # Remocao de campos legados (grupo/tema) do banco de dados
        for _leg in ("grupo", "tema"):
            try:
                _cols = [r[1] for r in c.execute("PRAGMA table_info(chat_sessions)").fetchall()]
                if _leg in _cols:
                    c.execute(f"ALTER TABLE chat_sessions DROP COLUMN {_leg}")
            except Exception:
                pass
        # Migracao leve: coluna folder_id em chat_sessions (pastas do historico)
        try:
            cols = [r[1] for r in c.execute("PRAGMA table_info(chat_sessions)").fetchall()]
            if "folder_id" not in cols:
                c.execute("ALTER TABLE chat_sessions ADD COLUMN folder_id TEXT")
        except Exception:
            pass
        # Migracao leve: coluna estilo em chat_messages (registro do estilo de resposta)
        try:
            mcols = [r[1] for r in c.execute("PRAGMA table_info(chat_messages)").fetchall()]
            if "estilo" not in mcols:
                c.execute("ALTER TABLE chat_messages ADD COLUMN estilo TEXT")
        except Exception:
            pass
        # Migracao leve: coluna modelo em chat_messages (local x gemini, por resposta)
        try:
            mcols2 = [r[1] for r in c.execute("PRAGMA table_info(chat_messages)").fetchall()]
            if "modelo" not in mcols2:
                c.execute("ALTER TABLE chat_messages ADD COLUMN modelo TEXT")
        except Exception:
            pass
        # Migracao leve: coluna estilo em user_prefs (estilo de resposta por conta)
        try:
            pcols = [r[1] for r in c.execute("PRAGMA table_info(user_prefs)").fetchall()]
            if "estilo" not in pcols:
                c.execute("ALTER TABLE user_prefs ADD COLUMN estilo TEXT DEFAULT 'equilibrado'")
        except Exception:
            pass
        # Anonimizacao: garante que nenhum nome real fique guardado no banco.
        c.execute("UPDATE chat_sessions SET user_name=NULL WHERE user_name IS NOT NULL")


# ------------------------------------------------------------------- Config (admin)
def get_config(chave, default=None):
    with _conn() as c:
        r = c.execute("SELECT valor FROM config WHERE chave=?", (chave,)).fetchone()
    return r["valor"] if r else default


def set_config(chave, valor):
    with _lock, _conn() as c:
        c.execute(
            "INSERT INTO config (chave,valor) VALUES (?,?) "
            "ON CONFLICT(chave) DO UPDATE SET valor=excluded.valor",
            (chave, str(valor)),
        )


def get_bool(chave, default=False):
    v = get_config(chave, None)
    if v is None:
        return default
    return str(v).lower() in ("1", "true", "yes", "on", "sim")


def clear_history():
    """Apaga TODAS as sessões, mensagens e ajustes registrados (não mexe nos documentos do RAG)."""
    with _lock, _conn() as c:
        c.execute("DELETE FROM chat_messages")
        c.execute("DELETE FROM chat_sessions")
        c.execute("DELETE FROM activity_log")


def delete_session_hard(session_id):
    """Apaga PERMANENTEMENTE uma conversa (sessão + mensagens) do banco — uso admin."""
    with _lock, _conn() as c:
        c.execute("DELETE FROM chat_messages WHERE session_id=?", (session_id,))
        c.execute("DELETE FROM chat_sessions WHERE id=?", (session_id,))


def add_activity(user_id, tipo, detalhe):
    """Registra um ajuste do usuário (ativar/desativar RAG, memória, mudar estilo)."""
    if not get_bool("gravar_no_bd", True):
        return {"gravado": False}
    aid = str(uuid.uuid4()); ts = now_iso()
    with _lock, _conn() as c:
        c.execute(
            "INSERT INTO activity_log (id,user_id,tipo,detalhe,created_at) VALUES (?,?,?,?,?)",
            (aid, user_id or "", (tipo or "")[:40], (detalhe or "")[:120], ts),
        )
    return {"id": aid, "created_at": ts}


def list_activities(limit=1000):
    with _conn() as c:
        rows = c.execute(
            "SELECT user_id,tipo,detalhe,created_at FROM activity_log ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["participante"] = anon_label(d.get("user_id"))
        out.append(d)
    return out


def list_all_sessions():
    """Todas as sessões (inclui ocultas) com contagem de mensagens — uso admin."""
    with _conn() as c:
        rows = c.execute(
            "SELECT s.id, s.user_id, s.user_name, s.title, s.created_at, "
            "       s.oculta_para_usuario, "
            "       (SELECT COUNT(*) FROM chat_messages m WHERE m.session_id = s.id) AS n_msgs, "
            "       (SELECT COUNT(*) FROM users u WHERE u.id = s.user_id) AS is_conta "
            "FROM chat_sessions s ORDER BY s.created_at DESC"
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["participante"] = anon_label(d.get("user_id"))
        d["user_name"] = d["participante"]
        d["tipo"] = "conta" if d.get("is_conta") else "visitante"
        out.append(d)
    return out


def stats():
    with _conn() as c:
        ns = c.execute("SELECT COUNT(*) AS n FROM chat_sessions").fetchone()["n"]
        nm = c.execute("SELECT COUNT(*) AS n FROM chat_messages").fetchone()["n"]
        nd = c.execute("SELECT COUNT(*) AS n FROM documentos").fetchone()["n"]
    return {"sessoes": ns, "mensagens": nm, "documentos": nd}


# ------------------------------------------------------------------ Sessoes
def anon_label(user_id):
    """Rotulo anonimo e estavel por participante (derivado do ID do navegador).
    Dois usuarios que digitem o mesmo nome recebem rotulos diferentes."""
    if not user_id:
        return "Participante ?"
    codigo = hashlib.sha1(str(user_id).encode("utf-8")).hexdigest()[:6].upper()
    return "Participante " + codigo


# ------------------------------------------------------------------ contas
def _hash_pw(password, salt_hex):
    return hashlib.pbkdf2_hmac(
        "sha256", (password or "").encode("utf-8"),
        bytes.fromhex(salt_hex), 120000
    ).hex()


# ------------------------------------------------- Senha do admin (trocavel na UI)
def admin_password_set():
    """True se o admin ja trocou a senha pela interface (guardada no BD)."""
    return bool(get_config("admin_pw_hash", ""))


def set_admin_password(new_password):
    salt = os.urandom(16).hex()
    set_config("admin_pw_hash", salt + "$" + _hash_pw(new_password, salt))


def verify_admin_password(password):
    stored = get_config("admin_pw_hash", "")
    if not stored:
        return False
    try:
        salt, ph = stored.split("$", 1)
    except ValueError:
        return False
    return _hash_pw(password, salt) == ph


def create_user(email, password, name=None):
    email = (email or "").strip().lower()
    if not email or not password:
        return {"error": "dados_incompletos"}
    salt = os.urandom(16).hex()
    stored = salt + "$" + _hash_pw(password, salt)
    uid = str(uuid.uuid4())
    nome = (name or "").strip() or email.split("@")[0]
    try:
        with _lock, _conn() as c:
            c.execute(
                "INSERT INTO users (id,email,pass_hash,name,created_at) VALUES (?,?,?,?,?)",
                (uid, email, stored, nome, now_iso()),
            )
    except sqlite3.IntegrityError:
        return {"error": "email_ja_cadastrado"}
    return {"id": uid, "email": email, "name": nome}


def verify_user(email, password):
    email = (email or "").strip().lower()
    with _conn() as c:
        row = c.execute(
            "SELECT id,email,pass_hash,name FROM users WHERE email=?", (email,)
        ).fetchone()
    if not row:
        return None
    try:
        salt, ph = row["pass_hash"].split("$", 1)
    except ValueError:
        return None
    if _hash_pw(password, salt) != ph:
        return None
    return {"id": row["id"], "email": row["email"], "name": row["name"]}


def get_user_by_id(user_id):
    with _conn() as c:
        row = c.execute("SELECT id,email,pass_hash,name FROM users WHERE id=?", (user_id,)).fetchone()
    return dict(row) if row else None


def verify_user_by_id(user_id, password):
    u = get_user_by_id(user_id)
    if not u:
        return False
    try:
        salt, ph = u["pass_hash"].split("$", 1)
    except ValueError:
        return False
    return _hash_pw(password, salt) == ph


def delete_user(user_id):
    """Exclui APENAS a conta (login) do usuario. O historico de conversas
    (chat_sessions/chat_messages) permanece no banco de forma anonima, para
    fins de pesquisa. Tambem removemos a memoria/preferencias pessoais, pois
    sao dados de personalizacao (nao fazem parte do historico de pesquisa)."""
    if not user_id:
        return {"error": "sem_usuario"}
    with _lock, _conn() as c:
        # Remove somente a conta e os dados pessoais de personalizacao.
        # As sessoes e mensagens NAO sao apagadas (dado de pesquisa preservado).
        c.execute("DELETE FROM user_prefs WHERE user_id=?", (user_id,))
        c.execute("DELETE FROM users WHERE id=?", (user_id,))
    return {"ok": True}


def list_users(limit=2000):
    """Contas cadastradas (uso admin): apenas email + data + nº de conversas."""
    with _conn() as c:
        rows = c.execute(
            "SELECT u.id, u.email, u.created_at, "
            "(SELECT COUNT(*) FROM chat_sessions s WHERE s.user_id = u.id) AS n_sessoes "
            "FROM users u ORDER BY u.created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def update_user(user_id, name=None, email=None, password=None):
    sets, vals = [], []
    if name is not None and name.strip():
        sets.append("name=?"); vals.append(name.strip()[:60])
    if email is not None and email.strip():
        sets.append("email=?"); vals.append(email.strip().lower())
    if password:
        salt = os.urandom(16).hex()
        sets.append("pass_hash=?"); vals.append(salt + "$" + _hash_pw(password, salt))
    if not sets:
        return {"ok": True, "nada": True}
    vals.append(user_id)
    try:
        with _lock, _conn() as c:
            c.execute(f"UPDATE users SET {', '.join(sets)} WHERE id=?", vals)
    except sqlite3.IntegrityError:
        return {"error": "email_ja_cadastrado"}
    u = get_user_by_id(user_id)
    return {"ok": True, "id": u["id"], "email": u["email"], "name": u["name"]}


# ------------------------------------------------------------ Preferencias/memoria
def get_prefs(user_id):
    with _conn() as c:
        row = c.execute(
            "SELECT memory_enabled,memory_ready,memory_text,msgs_since,estilo FROM user_prefs WHERE user_id=?",
            (user_id,),
        ).fetchone()
    if not row:
        return {"memory_enabled": False, "memory_ready": False, "memory_text": "", "msgs_since": 0, "estilo": "equilibrado"}
    return {
        "memory_enabled": bool(row["memory_enabled"]),
        "memory_ready": bool(row["memory_ready"]),
        "memory_text": row["memory_text"] or "",
        "msgs_since": row["msgs_since"] or 0,
        "estilo": row["estilo"] or "equilibrado",
    }


def _ensure_prefs(c, user_id):
    c.execute("INSERT OR IGNORE INTO user_prefs (user_id,updated_at) VALUES (?,?)", (user_id, now_iso()))


def set_estilo(user_id, estilo):
    est = estilo if estilo in ("mais_filosofico", "equilibrado", "menos_filosofico") else "equilibrado"
    with _lock, _conn() as c:
        _ensure_prefs(c, user_id)
        c.execute("UPDATE user_prefs SET estilo=?, updated_at=? WHERE user_id=?", (est, now_iso(), user_id))


def set_memory_enabled(user_id, enabled):
    with _lock, _conn() as c:
        _ensure_prefs(c, user_id)
        c.execute("UPDATE user_prefs SET memory_enabled=?, updated_at=? WHERE user_id=?",
                  (1 if enabled else 0, now_iso(), user_id))


def set_memory_text(user_id, text):
    txt = (text or "").strip()[:2000]
    with _lock, _conn() as c:
        _ensure_prefs(c, user_id)
        # edicao manual: se tem texto, considera pronta; se vazio, nao
        c.execute("UPDATE user_prefs SET memory_text=?, memory_ready=?, updated_at=? WHERE user_id=?",
                  (txt, 1 if txt else 0, now_iso(), user_id))


def save_curated_memory(user_id, text, ready):
    txt = (text or "").strip()[:2000]
    with _lock, _conn() as c:
        _ensure_prefs(c, user_id)
        c.execute("UPDATE user_prefs SET memory_text=?, memory_ready=?, msgs_since=0, updated_at=? WHERE user_id=?",
                  (txt, 1 if (ready and txt) else 0, now_iso(), user_id))


def bump_msgs_since(user_id):
    """Incrementa o contador de mensagens desde a ultima curadoria. Retorna o novo valor."""
    with _lock, _conn() as c:
        _ensure_prefs(c, user_id)
        c.execute("UPDATE user_prefs SET msgs_since=msgs_since+1 WHERE user_id=?", (user_id,))
        row = c.execute("SELECT msgs_since FROM user_prefs WHERE user_id=?", (user_id,)).fetchone()
    return row["msgs_since"] if row else 0


def get_user_messages(user_id, limit=40):
    """Ultimas mensagens do proprio usuario (role=user), das sessoes dele."""
    with _conn() as c:
        rows = c.execute(
            "SELECT m.content FROM chat_messages m "
            "JOIN chat_sessions s ON s.id=m.session_id "
            "WHERE s.user_id=? AND m.role='user' "
            "ORDER BY m.created_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    return [r["content"] for r in rows][::-1]


def create_session(user_id, title, user_name=None):
    sid = str(uuid.uuid4())
    ts = now_iso()
    user_name = (user_name or "").strip() or None  # visitante fica None (anonimo); usuario logado guarda o nome
    raw = (title or "").strip()
    titulo = (raw[:30] + "...") if len(raw) > 30 else (raw or "Nova conversa")
    if not get_bool("gravar_no_bd", True):
        # Gravação desligada no painel admin: sessão efêmera (não persiste)
        return {"id": sid, "user_id": user_id, "title": titulo,
                "user_name": user_name, "is_pinned": False,
                "created_at": ts, "gravado": False}
    with _lock, _conn() as c:
        c.execute(
            "INSERT INTO chat_sessions (id,user_id,title,user_name,is_pinned,oculta_para_usuario,created_at)"
            " VALUES (?,?,?,?,0,0,?)",
            (sid, user_id, titulo, user_name, ts),
        )
    return {
        "id": sid, "user_id": user_id, "title": titulo,
        "user_name": user_name, "is_pinned": False, "created_at": ts,
    }


def list_sessions(user_id):
    with _conn() as c:
        rows = c.execute(
            "SELECT id,title,is_pinned,folder_id,created_at FROM chat_sessions"
            " WHERE user_id=? AND oculta_para_usuario=0"
            " ORDER BY is_pinned DESC, created_at DESC",
            (user_id,),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["is_pinned"] = bool(d["is_pinned"])
        out.append(d)
    return out


def update_session(session_id, title=None, is_pinned=None, oculta=None, folder_id=Ellipsis):
    sets, vals = [], []
    if title is not None:
        sets.append("title=?"); vals.append(str(title)[:60])
    if is_pinned is not None:
        sets.append("is_pinned=?"); vals.append(1 if is_pinned else 0)
    if oculta is not None:
        sets.append("oculta_para_usuario=?"); vals.append(1 if oculta else 0)
    if folder_id is not Ellipsis:
        sets.append("folder_id=?"); vals.append(folder_id or None)
    if not sets:
        return
    vals.append(session_id)
    with _lock, _conn() as c:
        c.execute(f"UPDATE chat_sessions SET {', '.join(sets)} WHERE id=?", vals)


# ------------------------------------------------------------------ Pastas
def create_folder(user_id, name):
    if not user_id:
        return {"error": "sem_usuario"}
    nome = (name or "").strip()[:60] or "Nova pasta"
    fid = str(uuid.uuid4())
    with _lock, _conn() as c:
        c.execute(
            "INSERT INTO folders (id,user_id,name,created_at) VALUES (?,?,?,?)",
            (fid, user_id, nome, now_iso()),
        )
    return {"id": fid, "user_id": user_id, "name": nome}


def list_folders(user_id):
    with _conn() as c:
        rows = c.execute(
            "SELECT id,name,created_at FROM folders WHERE user_id=? ORDER BY name COLLATE NOCASE ASC",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def rename_folder(folder_id, name):
    nome = (name or "").strip()[:60]
    if not nome:
        return
    with _lock, _conn() as c:
        c.execute("UPDATE folders SET name=? WHERE id=?", (nome, folder_id))


def delete_folder(folder_id):
    # remove a pasta e solta as conversas (nao apaga as conversas)
    with _lock, _conn() as c:
        c.execute("UPDATE chat_sessions SET folder_id=NULL WHERE folder_id=?", (folder_id,))
        c.execute("DELETE FROM folders WHERE id=?", (folder_id,))


def set_session_folder(session_id, folder_id):
    with _lock, _conn() as c:
        c.execute("UPDATE chat_sessions SET folder_id=? WHERE id=?", (folder_id or None, session_id))


# ----------------------------------------------------------------- Mensagens
def add_message(session_id, role, content, used_rag=False, estilo=None, modelo=None):
    if role not in ("user", "assistant"):
        role = "assistant"
    mid = str(uuid.uuid4())
    ts = now_iso()
    if not get_bool("gravar_no_bd", True):
        return {"id": mid, "created_at": ts, "gravado": False}
    with _lock, _conn() as c:
        c.execute(
            "INSERT INTO chat_messages (id,session_id,role,content,used_rag,estilo,modelo,created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (mid, session_id, role, content, 1 if used_rag else 0, estilo, modelo, ts),
        )
    return {"id": mid, "created_at": ts}


def get_messages(session_id):
    with _conn() as c:
        rows = c.execute(
            "SELECT role,content,used_rag,estilo,modelo,created_at FROM chat_messages WHERE session_id=? ORDER BY created_at ASC",
            (session_id,),
        ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------- Exportar CSV
def _fmt_dt(iso_str):
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        try:
            from zoneinfo import ZoneInfo
            dt = dt.astimezone(ZoneInfo("America/Sao_Paulo"))
        except Exception:
            dt = dt.astimezone(timezone(timedelta(hours=-3)))
        return dt.strftime("%d/%m/%Y %H:%M:%S")
    except Exception:
        return iso_str or ""


def export_rows(user_id=None):
    """Retorna [cabecalho] + linhas, pareando cada pergunta com a resposta seguinte.
    Colunas: session_id, participante, pergunta, resposta, rag, estilo, modelo, data_hora."""
    q = "SELECT id,user_id,user_name,created_at FROM chat_sessions"
    params = []
    if user_id:
        q += " WHERE user_id=?"; params.append(user_id)
    q += " ORDER BY created_at ASC"
    rows = [["session_id", "participante", "pergunta", "resposta", "rag", "estilo", "modelo", "data_hora"]]
    with _conn() as c:
        sessions = c.execute(q, params).fetchall()
        for s in sessions:
            participante = s["user_name"] or anon_label(s["user_id"])
            msgs = c.execute(
                "SELECT role,content,used_rag,estilo,modelo,created_at FROM chat_messages WHERE session_id=? ORDER BY created_at ASC",
                (s["id"],),
            ).fetchall()
            i = 0
            while i < len(msgs):
                if msgs[i]["role"] == "user":
                    pergunta = msgs[i]["content"]
                    data_hora = _fmt_dt(msgs[i]["created_at"])
                    resposta = ""
                    rag_flag = ""
                    estilo_v = ""
                    modelo_v = ""
                    if i + 1 < len(msgs) and msgs[i + 1]["role"] == "assistant":
                        resposta = msgs[i + 1]["content"]
                        rag_flag = "sim" if msgs[i + 1]["used_rag"] else "nao"
                        estilo_v = msgs[i + 1]["estilo"] or "equilibrado"
                        modelo_v = msgs[i + 1]["modelo"] or "local"
                        i += 2
                    else:
                        i += 1
                    rows.append([s["id"], participante, pergunta, resposta, rag_flag, estilo_v, modelo_v, data_hora])
                else:
                    i += 1
    return rows


def export_csv(user_id=None, delimiter=";"):
    """Gera CSV a partir de export_rows (delimitador ';', padrao Excel pt-BR)."""
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=delimiter)
    for r in export_rows(user_id):
        w.writerow(r)
    return buf.getvalue()


# --------------------------------------------------------- Documentos (RAG)
def _emb_to_blob(vec):
    return np.asarray(vec, dtype=np.float32).tobytes()


def _blob_to_emb(blob):
    return np.frombuffer(blob, dtype=np.float32)


def clear_documents():
    global _vec_cache
    with _lock, _conn() as c:
        c.execute("DELETE FROM documentos")
    _vec_cache = None


def add_document(nome, conteudo, embedding):
    global _vec_cache
    did = str(uuid.uuid4())
    with _lock, _conn() as c:
        c.execute(
            "INSERT INTO documentos (id,nome_documento,conteudo,embedding) VALUES (?,?,?,?)",
            (did, nome, conteudo, _emb_to_blob(embedding)),
        )
    _vec_cache = None


def count_documents():
    with _conn() as c:
        return c.execute("SELECT COUNT(*) AS n FROM documentos").fetchone()["n"]


def _load_vectors():
    global _vec_cache
    if _vec_cache is not None:
        return _vec_cache
    ids, conts, mats = [], [], []
    with _conn() as c:
        for r in c.execute("SELECT id,conteudo,embedding FROM documentos"):
            ids.append(r["id"]); conts.append(r["conteudo"]); mats.append(_blob_to_emb(r["embedding"]))
    if mats:
        M = np.vstack(mats).astype(np.float32)
        norms = np.linalg.norm(M, axis=1, keepdims=True)
        norms[norms == 0] = 1e-9
        Mn = M / norms
    else:
        Mn = np.zeros((0, 0), dtype=np.float32)
    _vec_cache = (ids, conts, Mn)
    return _vec_cache


def search_documents(query_embedding, threshold=0.55, count=3):
    """Busca por similaridade de cosseno em Python. Corpus pequeno -> instantaneo."""
    if np is None:
        return []
    ids, conts, Mn = _load_vectors()
    if len(conts) == 0:
        return []
    q = np.asarray(query_embedding, dtype=np.float32)
    qn = q / (np.linalg.norm(q) or 1e-9)
    sims = Mn @ qn
    order = np.argsort(-sims)
    res = []
    for idx in order:
        if float(sims[idx]) < threshold:
            break
        res.append({"conteudo": conts[idx], "similaridade": float(sims[idx])})
        if len(res) >= count:
            break
    return res
