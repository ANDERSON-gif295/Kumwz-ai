import os,secrets,time
from functools import wraps
from flask import Flask,request,jsonify,g,render_template
from werkzeug.security import generate_password_hash,check_password_hash
import requests

app=Flask(__name__)

DATABASE_URL=os.environ.get("DATABASE_URL")
USE_POSTGRES=bool(DATABASE_URL)

if USE_POSTGRES:
    import psycopg2
    import psycopg2.extras
else:
    import sqlite3

DB_PATH=os.path.join(os.path.dirname(os.path.abspath(__file__)),"kumwz.db") if not USE_POSTGRES else None

def get_db():
    if "db" not in g:
        if USE_POSTGRES:
            g.db=psycopg2.connect(DATABASE_URL)
            g.db.cursor_factory=psycopg2.extras.RealDictCursor
        else:
            g.db=sqlite3.connect(DB_PATH)
            g.db.row_factory=sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    db=g.pop("db",None)
    if db:db.close()

def init_db():
    conn=get_db()
    cursor=conn.cursor()
    if USE_POSTGRES:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS api_keys(
                key TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                created_at DOUBLE PRECISION NOT NULL,
                active INTEGER NOT NULL DEFAULT 1
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS requests_log(
                id SERIAL PRIMARY KEY,
                api_key TEXT NOT NULL,
                provider TEXT NOT NULL,
                model TEXT,
                prompt_tokens INTEGER,
                completion_tokens INTEGER,
                total_tokens INTEGER,
                status TEXT NOT NULL,
                latency_ms INTEGER,
                created_at DOUBLE PRECISION NOT NULL,
                error TEXT
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users(
                email TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                api_key TEXT NOT NULL,
                created_at DOUBLE PRECISION NOT NULL
            )
        """)
        conn.commit()
    else:
        cursor.execute("CREATE TABLE IF NOT EXISTS api_keys(key TEXT PRIMARY KEY,owner TEXT NOT NULL,created_at REAL NOT NULL,active INTEGER NOT NULL DEFAULT 1)")
        cursor.execute("CREATE TABLE IF NOT EXISTS requests_log(id INTEGER PRIMARY KEY AUTOINCREMENT,api_key TEXT NOT NULL,provider TEXT NOT NULL,model TEXT,prompt_tokens INTEGER,completion_tokens INTEGER,total_tokens INTEGER,status TEXT NOT NULL,latency_ms INTEGER,created_at REAL NOT NULL,error TEXT)")
        cursor.execute("CREATE TABLE IF NOT EXISTS users(email TEXT PRIMARY KEY,name TEXT NOT NULL,password_hash TEXT NOT NULL,api_key TEXT NOT NULL,created_at REAL NOT NULL)")
        conn.commit()
    cursor.close()

def require_api_key(f):
    @wraps(f)
    def wrapper(*args,**kwargs):
        key=request.headers.get("X-API-Key") or request.args.get("api_key")
        if not key:return jsonify({"error":"Missing API key"}),401
        db=get_db()
        cursor=db.cursor()
        cursor.execute("SELECT * FROM api_keys WHERE key=%s AND active=1" if USE_POSTGRES else "SELECT * FROM api_keys WHERE key=? AND active=1",(key,))
        row=cursor.fetchone()
        cursor.close()
        if not row:return jsonify({"error":"Invalid API key"}),403
        g.api_key=key;g.api_owner=row["owner"]
        return f(*args,**kwargs)
    return wrapper

def call_groq(model,messages):
    k=os.environ.get("GROQ_API_KEY")
    if not k:raise RuntimeError("GROQ_API_KEY not set")
    r=requests.post("https://api.groq.com/openai/v1/chat/completions",headers={"Authorization":f"Bearer {k}","Content-Type":"application/json"},json={"model":model or "llama-3.1-8b-instant","messages":messages},timeout=60)
    r.raise_for_status();d=r.json();u=d.get("usage",{})
    return{"text":d["choices"][0]["message"]["content"],"prompt_tokens":u.get("prompt_tokens",0),"completion_tokens":u.get("completion_tokens",0),"total_tokens":u.get("total_tokens",0),"raw_model":d.get("model",model)}

def call_openai(model,messages):
    k=os.environ.get("OPENAI_API_KEY")
    if not k:raise RuntimeError("OPENAI_API_KEY not set")
    r=requests.post("https://api.openai.com/v1/chat/completions",headers={"Authorization":f"Bearer {k}","Content-Type":"application/json"},json={"model":model or "gpt-4o-mini","messages":messages},timeout=60)
    r.raise_for_status();d=r.json();u=d.get("usage",{})
    return{"text":d["choices"][0]["message"]["content"],"prompt_tokens":u.get("prompt_tokens",0),"completion_tokens":u.get("completion_tokens",0),"total_tokens":u.get("total_tokens",0),"raw_model":d.get("model",model)}

def call_anthropic(model,messages):
    k=os.environ.get("ANTHROPIC_API_KEY")
    if not k:raise RuntimeError("ANTHROPIC_API_KEY not set")
    sys_msg=None;converted=[]
    for m in messages:
        if m["role"]=="system":sys_msg=m["content"]
        else:converted.append({"role":m["role"],"content":m["content"]})
    payload={"model":model or "claude-sonnet-4-6","max_tokens":1000,"messages":converted}
    if sys_msg:payload["system"]=sys_msg
    r=requests.post("https://api.anthropic.com/v1/messages",headers={"x-api-key":k,"anthropic-version":"2023-06-01","Content-Type":"application/json"},json=payload,timeout=60)
    r.raise_for_status();d=r.json()
    text="".join(b.get("text","") for b in d.get("content",[]) if b.get("type")=="text")
    u=d.get("usage",{})
    return{"text":text,"prompt_tokens":u.get("input_tokens",0),"completion_tokens":u.get("output_tokens",0),"total_tokens":u.get("input_tokens",0)+u.get("output_tokens",0),"raw_model":d.get("model",model)}

PROVIDERS={"groq":call_groq,"openai":call_openai,"anthropic":call_anthropic}

@app.route("/")
def index():
    admin_secret=os.environ.get("ADMIN_SECRET","changeme")
    return render_template("index.html",admin_secret=admin_secret)

@app.route("/health")
def health():return jsonify({"status":"ok","time":time.time()})

@app.route("/v1/chat",methods=["POST"])
@require_api_key
def chat():
    body=request.get_json(silent=True) or {}
    provider=(body.get("provider") or "groq").lower()
    model=body.get("model");messages=body.get("messages")
    system_msg={"role":"system","content":"You are a helpful AI assistant. If you don't understand a question, ask for clarification instead of making up answers. Be direct and factual. Avoid lengthy explanations unless asked."}
    messages=[system_msg]+messages
    if not messages:return jsonify({"error":"messages required"}),400
    if provider not in PROVIDERS:return jsonify({"error":f"Unknown provider. Use: {list(PROVIDERS.keys())}"}),400
    start=time.time();db=get_db()
    cursor=db.cursor()
    try:
        result=PROVIDERS[provider](model,messages)
        ms=int((time.time()-start)*1000)
        placeholder="%s" if USE_POSTGRES else "?"
        cursor.execute(f"INSERT INTO requests_log(api_key,provider,model,prompt_tokens,completion_tokens,total_tokens,status,latency_ms,created_at,error) VALUES({placeholder},{placeholder},{placeholder},{placeholder},{placeholder},{placeholder},{placeholder},{placeholder},{placeholder},NULL)",(g.api_key,provider,result.get("raw_model",model),result["prompt_tokens"],result["completion_tokens"],result["total_tokens"],"success",ms,time.time()))
        db.commit()
        return jsonify({"provider":provider,"model":result.get("raw_model",model),"text":result["text"],"usage":{"prompt_tokens":result["prompt_tokens"],"completion_tokens":result["completion_tokens"],"total_tokens":result["total_tokens"]},"latency_ms":ms})
    except Exception as e:
        ms=int((time.time()-start)*1000)
        placeholder="%s" if USE_POSTGRES else "?"
        cursor.execute(f"INSERT INTO requests_log(api_key,provider,model,prompt_tokens,completion_tokens,total_tokens,status,latency_ms,created_at,error) VALUES({placeholder},{placeholder},{placeholder},0,0,0,{placeholder},{placeholder},{placeholder},{placeholder})",(g.api_key,provider,model,"error",ms,time.time(),str(e)))
        db.commit()
        cursor.close()
        return jsonify({"error":str(e)}),502
    finally:
        cursor.close()

@app.route("/v1/usage")
@require_api_key
def usage():
    db=get_db()
    cursor=db.cursor()
    placeholder="%s" if USE_POSTGRES else "?"
    cursor.execute(f"SELECT provider,COUNT(*) as requests,SUM(total_tokens) as total_tokens,SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) as errors FROM requests_log WHERE api_key={placeholder} GROUP BY provider",(g.api_key,))
    rows=cursor.fetchall()
    cursor.close()
    return jsonify({"owner":g.api_owner,"usage_by_provider":[dict(r) for r in rows]})

@app.route("/admin/keys",methods=["POST"])
def create_key():
    if request.headers.get("X-Admin-Secret")!=os.environ.get("ADMIN_SECRET","changeme"):return jsonify({"error":"unauthorized"}),403
    body=request.get_json(silent=True) or {}
    key="kw_"+secrets.token_hex(16)
    db=get_db()
    cursor=db.cursor()
    placeholder="%s" if USE_POSTGRES else "?"
    cursor.execute(f"INSERT INTO api_keys(key,owner,created_at,active) VALUES({placeholder},{placeholder},{placeholder},1)",(key,body.get("owner","unknown"),time.time()))
    db.commit()
    cursor.close()
    return jsonify({"api_key":key,"owner":body.get("owner","unknown")})

@app.route("/api/signup",methods=["POST"])
def signup():
    body=request.get_json(silent=True) or {}
    name=(body.get("name") or "").strip()
    email=(body.get("email") or "").strip().lower()
    password=body.get("password") or ""
    if not name or not email or not password:
        return jsonify({"error":"Fill in all fields."}),400
    if "@" not in email:
        return jsonify({"error":"Enter a valid email."}),400
    if len(password)<6:
        return jsonify({"error":"Password must be at least 6 characters."}),400
    db=get_db()
    cursor=db.cursor()
    placeholder="%s" if USE_POSTGRES else "?"
    cursor.execute(f"SELECT email FROM users WHERE email={placeholder}",(email,))
    existing=cursor.fetchone()
    if existing:
        cursor.close()
        return jsonify({"error":"Account already exists. Sign in instead."}),409
    api_key="kw_"+secrets.token_hex(16)
    pw_hash=generate_password_hash(password)
    now=time.time()
    cursor.execute(f"INSERT INTO users(email,name,password_hash,api_key,created_at) VALUES({placeholder},{placeholder},{placeholder},{placeholder},{placeholder})",(email,name,pw_hash,api_key,now))
    cursor.execute(f"INSERT INTO api_keys(key,owner,created_at,active) VALUES({placeholder},{placeholder},{placeholder},1)",(api_key,name,now))
    db.commit()
    cursor.close()
    return jsonify({"name":name,"email":email,"api_key":api_key})

@app.route("/api/login",methods=["POST"])
def login():
    body=request.get_json(silent=True) or {}
    email=(body.get("email") or "").strip().lower()
    password=body.get("password") or ""
    if not email or not password:
        return jsonify({"error":"Fill in both fields."}),400
    db=get_db()
    cursor=db.cursor()
    placeholder="%s" if USE_POSTGRES else "?"
    cursor.execute(f"SELECT * FROM users WHERE email={placeholder}",(email,))
    user=cursor.fetchone()
    cursor.close()
    if not user:
        return jsonify({"error":"No account found with this email."}),404
    if not check_password_hash(user["password_hash"],password):
        return jsonify({"error":"Incorrect password."}),401
    return jsonify({"name":user["name"],"email":user["email"],"api_key":user["api_key"]})

init_db()

if __name__=="__main__":
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT",8080)),debug=False)

@app.route("/admin")
def admin():
    return render_template("admin.html")

@app.route("/admin/dashboard")
def admin_dashboard():
    if request.headers.get("X-Admin-Secret") != os.environ.get("ADMIN_SECRET","changeme"):
        return jsonify({"error":"unauthorized"}),403
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT * FROM api_keys ORDER BY created_at DESC")
    keys = cursor.fetchall()
    result = []
    for k in keys:
        placeholder="%s" if USE_POSTGRES else "?"
        cursor.execute(
            f"SELECT COUNT(*) as total_requests, SUM(total_tokens) as total_tokens, SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) as errors FROM requests_log WHERE api_key={placeholder}",
            (k["key"],)
        )
        stats = cursor.fetchone()
        result.append({
            "key": k["key"],
            "owner": k["owner"],
            "active": bool(k["active"]),
            "created_at": k["created_at"],
            "total_requests": stats["total_requests"] or 0,
            "total_tokens": stats["total_tokens"] or 0,
            "errors": stats["errors"] or 0,
        })
    cursor.close()
    return jsonify({"keys": result})

@app.route("/admin/keys/toggle", methods=["POST"])
def toggle_key():
    if request.headers.get("X-Admin-Secret") != os.environ.get("ADMIN_SECRET","changeme"):
        return jsonify({"error":"unauthorized"}),403
    body = request.get_json(silent=True) or {}
    db = get_db()
    cursor = db.cursor()
    placeholder="%s" if USE_POSTGRES else "?"
    cursor.execute(f"UPDATE api_keys SET active={placeholder} WHERE key={placeholder}",
               (1 if body.get("active") else 0, body.get("key")))
    db.commit()
    cursor.close()
    return jsonify({"ok": True})
