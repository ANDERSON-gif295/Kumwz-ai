import os,sqlite3,secrets,time,json
from functools import wraps
from flask import Flask,request,jsonify,g
import requests

DB_PATH=os.path.join(os.path.dirname(os.path.abspath(__file__)),"kumwz.db")
app=Flask(__name__)

def get_db():
    if "db" not in g:
        g.db=sqlite3.connect(DB_PATH)
        g.db.row_factory=sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    db=g.pop("db",None)
    if db:db.close()

def init_db():
    conn=sqlite3.connect(DB_PATH)
    conn.execute("CREATE TABLE IF NOT EXISTS api_keys(key TEXT PRIMARY KEY,owner TEXT NOT NULL,created_at REAL NOT NULL,active INTEGER NOT NULL DEFAULT 1)")
    conn.execute("CREATE TABLE IF NOT EXISTS requests_log(id INTEGER PRIMARY KEY AUTOINCREMENT,api_key TEXT NOT NULL,provider TEXT NOT NULL,model TEXT,prompt_tokens INTEGER,completion_tokens INTEGER,total_tokens INTEGER,status TEXT NOT NULL,latency_ms INTEGER,created_at REAL NOT NULL,error TEXT)")
    conn.commit();conn.close()

def require_api_key(f):
    @wraps(f)
    def wrapper(*args,**kwargs):
        key=request.headers.get("X-API-Key") or request.args.get("api_key")
        if not key:return jsonify({"error":"Missing API key"}),401
        db=get_db()
        row=db.execute("SELECT * FROM api_keys WHERE key=? AND active=1",(key,)).fetchone()
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

@app.route("/health")
def health():return jsonify({"status":"ok","time":time.time()})

@app.route("/v1/chat",methods=["POST"])
@require_api_key
def chat():
    body=request.get_json(silent=True) or {}
    provider=(body.get("provider") or "groq").lower()
    model=body.get("model");messages=body.get("messages")
    if not messages:return jsonify({"error":"messages required"}),400
    if provider not in PROVIDERS:return jsonify({"error":f"Unknown provider. Use: {list(PROVIDERS.keys())}"}),400
    start=time.time();db=get_db()
    try:
        result=PROVIDERS[provider](model,messages)
        ms=int((time.time()-start)*1000)
        db.execute("INSERT INTO requests_log(api_key,provider,model,prompt_tokens,completion_tokens,total_tokens,status,latency_ms,created_at,error) VALUES(?,?,?,?,?,?,'success',?,?,NULL)",(g.api_key,provider,result.get("raw_model",model),result["prompt_tokens"],result["completion_tokens"],result["total_tokens"],ms,time.time()))
        db.commit()
        return jsonify({"provider":provider,"model":result.get("raw_model",model),"text":result["text"],"usage":{"prompt_tokens":result["prompt_tokens"],"completion_tokens":result["completion_tokens"],"total_tokens":result["total_tokens"]},"latency_ms":ms})
    except Exception as e:
        ms=int((time.time()-start)*1000)
        db.execute("INSERT INTO requests_log(api_key,provider,model,prompt_tokens,completion_tokens,total_tokens,status,latency_ms,created_at,error) VALUES(?,?,?,0,0,0,'error',?,?,?)",(g.api_key,provider,model,ms,time.time(),str(e)))
        db.commit();return jsonify({"error":str(e)}),502

@app.route("/v1/usage")
@require_api_key
def usage():
    db=get_db()
    rows=db.execute("SELECT provider,COUNT(*) as requests,SUM(total_tokens) as total_tokens,SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) as errors FROM requests_log WHERE api_key=? GROUP BY provider",(g.api_key,)).fetchall()
    return jsonify({"owner":g.api_owner,"usage_by_provider":[dict(r) for r in rows]})

@app.route("/admin/keys",methods=["POST"])
def create_key():
    if request.headers.get("X-Admin-Secret")!=os.environ.get("ADMIN_SECRET","changeme"):return jsonify({"error":"unauthorized"}),403
    body=request.get_json(silent=True) or {}
    key="kw_"+secrets.token_hex(16)
    db=get_db()
    db.execute("INSERT INTO api_keys(key,owner,created_at,active) VALUES(?,?,?,1)",(key,body.get("owner","unknown"),time.time()))
    db.commit();return jsonify({"api_key":key,"owner":body.get("owner","unknown")})

if __name__=="__main__":
    init_db()
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT",8080)),debug=False)
