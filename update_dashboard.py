"""
update_dashboard.py
─────────────────────────────────────────────────────────────
Busca os dados direto do Databricks (via Statement Execution API)
e regenera o index.html do dashboard com os valores atualizados.

Roda localmente (para teste) OU dentro do GitHub Actions (automático).

Variáveis de ambiente necessárias:
  DATABRICKS_HOST        ex: https://dbc-xxxxxxxx-yyyy.cloud.databricks.com
  DATABRICKS_TOKEN       o PAT gerado na tela "Gerar novo token"
  DATABRICKS_WAREHOUSE_ID  o ID do SQL Warehouse
─────────────────────────────────────────────────────────────
"""

import os
import json
import time
import sys
from datetime import datetime
import urllib.request
import urllib.error

# ── 1. Configuração ──────────────────────────────────────────────────────────
DATABRICKS_HOST = os.environ.get("DATABRICKS_HOST", "").rstrip("/")
DATABRICKS_TOKEN = os.environ.get("DATABRICKS_TOKEN", "")
WAREHOUSE_ID = os.environ.get("DATABRICKS_WAREHOUSE_ID", "")

if not all([DATABRICKS_HOST, DATABRICKS_TOKEN, WAREHOUSE_ID]):
    print("❌ Faltam variáveis de ambiente. Configure:")
    print("   DATABRICKS_HOST, DATABRICKS_TOKEN, DATABRICKS_WAREHOUSE_ID")
    sys.exit(1)

# Query que junta sistemas + KPIs mais recentes (mesma lógica do Script 3)
SQL_QUERY = """
WITH historico AS (
  SELECT
    sistema_id,
    array_join(
      transform(
        sort_array(collect_list(struct(periodo, score_governanca))),
        x -> CAST(x.score_governanca AS STRING)
      ), ','
    ) AS score_hist_str
  FROM governanca.kpis_mensais
  GROUP BY sistema_id
),
ultimo_periodo AS (
  SELECT sistema_id, MAX(periodo) AS periodo_max
  FROM governanca.kpis_mensais
  GROUP BY sistema_id
)
SELECT
  s.sistema_id   AS id,
  s.nome_sistema AS nome,
  s.tipo,
  s.dominio,
  s.data_owner   AS owner,
  s.data_steward AS steward,
  s.criticidade,
  k.score_governanca  AS score,
  h.score_hist_str,
  k.pct_metadados,
  k.pct_retencao,
  k.nao_conformidades
FROM governanca.sistemas s
JOIN ultimo_periodo up ON s.sistema_id = up.sistema_id
JOIN governanca.kpis_mensais k
  ON k.sistema_id = up.sistema_id AND k.periodo = up.periodo_max
JOIN historico h ON h.sistema_id = s.sistema_id
ORDER BY s.sistema_id
"""

# ── 2. Funções de chamada à API REST do Databricks ──────────────────────────
def api_request(path, payload=None, method="POST"):
    url = f"{DATABRICKS_HOST}{path}"
    data = json.dumps(payload).encode() if payload else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {DATABRICKS_TOKEN}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"❌ Erro HTTP {e.code}: {e.read().decode()}")
        sys.exit(1)


def run_query(sql):
    """Executa a query via Statement Execution API e aguarda o resultado."""
    print("📡 Enviando query ao Databricks SQL Warehouse...")
    payload = {
        "warehouse_id": WAREHOUSE_ID,
        "statement": sql,
        "wait_timeout": "30s",
    }
    result = api_request("/api/2.0/sql/statements", payload)
    statement_id = result["statement_id"]
    status = result["status"]["state"]

    # Poll até finalizar (caso wait_timeout não seja suficiente)
    while status in ("PENDING", "RUNNING"):
        print(f"   ...status: {status}, aguardando")
        time.sleep(2)
        result = api_request(f"/api/2.0/sql/statements/{statement_id}", method="GET")
        status = result["status"]["state"]

    if status != "SUCCEEDED":
        print(f"❌ Query falhou com status: {status}")
        print(json.dumps(result.get("status", {}), indent=2))
        sys.exit(1)

    print("✅ Query executada com sucesso")
    return result


def parse_result_to_dicts(result):
    """Converte o resultado bruto da API em lista de dicionários Python."""
    columns = [c["name"] for c in result["manifest"]["schema"]["columns"]]
    rows = result["result"]["data_array"]

    sistemas = []
    for row in rows:
        record = dict(zip(columns, row))
        score_hist = [int(x) for x in record["score_hist_str"].split(",")]
        sistemas.append({
            "id": record["id"],
            "nome": record["nome"],
            "tipo": record["tipo"],
            "dominio": record["dominio"],
            "owner": record["owner"],
            "steward": record["steward"],
            "criticidade": record["criticidade"],
            "score": int(record["score"]),
            "score_hist": score_hist,
            "pct_metadados": float(record["pct_metadados"]),
            "pct_retencao": float(record["pct_retencao"]),
            "nao_conformidades": int(record["nao_conformidades"]),
        })
    return sistemas


# ── 3. Geração do HTML (mesma lógica usada antes) ────────────────────────────
def build_html(sistemas):
    MESES = ["Jan", "Fev", "Mar", "Abr", "Mai", "Jun"]

    for s in sistemas:
        score = s["score"]
        s["status_gov"] = "Conforme" if score >= 80 else ("Parcial" if score >= 60 else "Não Conforme")
        pct_ret = s["pct_retencao"]
        s["status_ret"] = "Ativo" if pct_ret > 75 else ("Revisão Pendente" if pct_ret > 50 else "Vencido")
        s["data_ultima_revisao"] = datetime.now().strftime("%d/%m/%Y")

    total = len(sistemas)
    conformes = sum(1 for s in sistemas if s["status_gov"] == "Conforme")
    parciais  = sum(1 for s in sistemas if s["status_gov"] == "Parcial")
    nao_conf  = sum(1 for s in sistemas if s["status_gov"] == "Não Conforme")
    score_medio = round(sum(s["score"] for s in sistemas) / total, 1)
    total_nc = sum(s["nao_conformidades"] for s in sistemas)
    com_owner = sum(1 for s in sistemas if s["owner"])
    pct_owner = round(com_owner / total * 100)
    pct_meta_avg = round(sum(s["pct_metadados"] for s in sistemas) / total, 1)
    pct_ret_avg = round(sum(s["pct_retencao"] for s in sistemas) / total, 1)
    score_evo_medio = [round(sum(s["score_hist"][i] for s in sistemas) / total, 1) for i in range(6)]

    by_dominio = {}
    for s in sistemas:
        by_dominio.setdefault(s["dominio"], []).append(s["score"])
    dominio_labels = list(by_dominio.keys())
    dominio_scores = [round(sum(v) / len(v), 1) for v in by_dominio.values()]
    dominio_counts = [len(v) for v in by_dominio.values()]

    status_dist = [conformes, parciais, nao_conf]
    ret_dist = [
        sum(1 for s in sistemas if s["status_ret"] == "Ativo"),
        sum(1 for s in sistemas if s["status_ret"] == "Revisão Pendente"),
        sum(1 for s in sistemas if s["status_ret"] == "Vencido"),
    ]

    sistemas_json    = json.dumps(sistemas, ensure_ascii=False)
    meses_json       = json.dumps(MESES)
    score_evo_json   = json.dumps(score_evo_medio)
    dom_labels_json  = json.dumps(dominio_labels)
    dom_scores_json  = json.dumps(dominio_scores)
    dom_counts_json  = json.dumps(dominio_counts)
    status_dist_json = json.dumps(status_dist)
    ret_dist_json    = json.dumps(ret_dist)
    gerado_em        = datetime.now().strftime("%d/%m/%Y %H:%M")

    html = f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Governança Federada — Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  html, body {{
    font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
    background: #F0F4F8; color: #1E293B; min-height: 100vh;
    margin: 0 !important; padding: 0 !important;
  }}
  .layout {{ display: flex; min-height: 100vh; }}
  .sidebar {{
    width: 220px; min-width: 220px; background: #0F1E35;
    display: flex; flex-direction: column;
    position: fixed; top: 0; left: 0; bottom: 0;
    z-index: 100; overflow-y: auto; transition: transform 0.3s;
  }}
  .sidebar-logo {{
    padding: 20px 18px 16px; border-bottom: 1px solid #243347;
    display: flex; align-items: center; gap: 10px;
  }}
  .logo-icon {{
    width: 36px; height: 36px; background: #2563EB; border-radius: 8px;
    display: flex; align-items: center; justify-content: center;
    font-size: 18px; flex-shrink: 0;
  }}
  .logo-text {{
    font-size: 11px; font-weight: 700; color: #fff;
    text-transform: uppercase; letter-spacing: 0.5px; line-height: 1.4;
  }}
  .nav-section {{ padding: 12px 0; flex: 1; }}
  .nav-label {{
    padding: 6px 18px; font-size: 9px; font-weight: 700;
    color: #475569; text-transform: uppercase; letter-spacing: 1.5px;
  }}
  .nav-item {{
    display: flex; align-items: center; gap: 10px; padding: 10px 18px;
    font-size: 12.5px; cursor: pointer; color: #94A3B8;
    border-left: 3px solid transparent; transition: all 0.15s;
    text-decoration: none; background: transparent;
    border-right: none; border-top: none; border-bottom: none;
    width: 100%; text-align: left;
  }}
  .nav-item:hover {{ color: #fff; background: rgba(255,255,255,0.05); }}
  .nav-item.active {{
    color: #fff; background: #2563EB;
    border-left-color: #60A5FA; font-weight: 700;
  }}
  .nav-item .nav-icon {{ font-size: 15px; width: 20px; text-align: center; }}
  .sidebar-footer {{
    padding: 14px 18px; border-top: 1px solid #243347;
    font-size: 10px; color: #475569; line-height: 1.6;
  }}
  .main {{ margin-left: 220px; flex: 1; display: flex; flex-direction: column; min-height: 100vh; }}
  .topbar {{
    background: #fff; padding: 14px 28px; border-bottom: 1px solid #E2E8F0;
    display: flex; align-items: center; justify-content: space-between;
    flex-wrap: wrap; gap: 10px; position: sticky; top: 0; z-index: 50;
  }}
  .topbar-title {{ font-size: 18px; font-weight: 800; color: #0F1E35; }}
  .topbar-sub {{ font-size: 11px; color: #64748B; margin-top: 2px; }}
  .topbar-right {{ display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }}
  .badge-update {{
    font-size: 10px; color: #64748B; background: #F1F5F9;
    padding: 5px 12px; border-radius: 20px; border: 1px solid #E2E8F0;
  }}
  .badge-source {{
    font-size: 10px; color: #16A34A; background: #DCFCE7;
    padding: 5px 12px; border-radius: 20px; border: 1px solid #BBF7D0; font-weight:700;
  }}
  .filter-select {{
    font-size: 11px; padding: 5px 10px; border: 1px solid #E2E8F0;
    border-radius: 8px; background: #F8FAFC; color: #1E293B; cursor: pointer;
  }}
  .page {{ display: none; padding: 24px 28px; }}
  .page.active {{ display: block; }}
  .kpi-grid {{
    display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
    gap: 14px; margin-bottom: 24px;
  }}
  .kpi-card {{
    background: #fff; border-radius: 12px; padding: 18px 20px;
    border: 1px solid #E2E8F0; box-shadow: 0 1px 4px rgba(0,0,0,0.05);
    display: flex; flex-direction: column; gap: 6px;
  }}
  .kpi-icon {{ font-size: 22px; }}
  .kpi-label {{ font-size: 11px; color: #64748B; font-weight: 600; line-height: 1.3; }}
  .kpi-value {{ font-size: 30px; font-weight: 800; color: #0F1E35; line-height: 1; }}
  .kpi-sub {{ font-size: 11px; font-weight: 600; }}
  .kpi-green {{ color: #16A34A; }}
  .kpi-amber {{ color: #D97706; }}
  .kpi-red   {{ color: #DC2626; }}
  .kpi-blue  {{ color: #2563EB; }}
  .charts-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 24px; }}
  .charts-grid-3 {{ display: grid; grid-template-columns: 2fr 1fr 1fr; gap: 16px; margin-bottom: 24px; }}
  .chart-card {{
    background: #fff; border-radius: 12px; padding: 20px;
    border: 1px solid #E2E8F0; box-shadow: 0 1px 4px rgba(0,0,0,0.05);
  }}
  .chart-card.full {{ grid-column: 1 / -1; }}
  .chart-title {{
    font-size: 12px; font-weight: 700; color: #0F1E35; margin-bottom: 14px;
    display: flex; justify-content: space-between; align-items: center;
  }}
  .chart-title span {{ font-size: 10px; font-weight: 400; color: #64748B; }}
  canvas {{ max-height: 220px; }}
  .table-card {{
    background: #fff; border-radius: 12px; border: 1px solid #E2E8F0;
    box-shadow: 0 1px 4px rgba(0,0,0,0.05); overflow: hidden; margin-bottom: 24px;
  }}
  .table-header {{
    padding: 16px 20px; display: flex; justify-content: space-between;
    align-items: center; border-bottom: 1px solid #F1F5F9; flex-wrap: wrap; gap: 10px;
  }}
  .table-title {{ font-size: 13px; font-weight: 700; color: #0F1E35; }}
  .search-box {{
    padding: 6px 12px; border: 1px solid #E2E8F0; border-radius: 8px;
    font-size: 12px; background: #F8FAFC; width: 220px;
  }}
  .table-wrap {{ overflow-x: auto; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 11px; }}
  thead th {{
    background: #F8FAFC; padding: 10px 14px; text-align: left;
    color: #64748B; font-weight: 700; font-size: 10px;
    text-transform: uppercase; letter-spacing: 0.5px;
    border-bottom: 1px solid #E2E8F0; white-space: nowrap;
  }}
  tbody tr {{ border-bottom: 1px solid #F1F5F9; transition: background 0.1s; }}
  tbody tr:hover {{ background: #F8FAFC; }}
  tbody td {{ padding: 10px 14px; color: #334155; }}
  tbody td:first-child {{ font-weight: 700; color: #0F1E35; }}
  .table-footer {{
    padding: 10px 20px; font-size: 10px; color: #64748B;
    border-top: 1px solid #F1F5F9; background: #FAFAFA;
  }}
  .badge {{
    display: inline-block; padding: 2px 10px; border-radius: 20px;
    font-size: 10px; font-weight: 700; white-space: nowrap;
  }}
  .badge-green  {{ background: #DCFCE7; color: #16A34A; }}
  .badge-amber  {{ background: #FEF3C7; color: #D97706; }}
  .badge-red    {{ background: #FEE2E2; color: #DC2626; }}
  .badge-blue   {{ background: #DBEAFE; color: #2563EB; }}
  .badge-gray   {{ background: #F1F5F9; color: #64748B; }}
  .score-bar-wrap {{ width: 80px; display: inline-block; vertical-align: middle; }}
  .score-bar-bg {{ height: 6px; background: #E2E8F0; border-radius: 3px; overflow: hidden; }}
  .score-bar-fill {{ height: 100%; border-radius: 3px; }}
  .sidebar-toggle {{
    display: none; position: fixed; top: 14px; left: 14px; z-index: 200;
    background: #2563EB; color: #fff; border: none; border-radius: 8px;
    width: 36px; height: 36px; font-size: 18px; cursor: pointer;
  }}
  @media (max-width: 768px) {{
    .sidebar {{ transform: translateX(-220px); }}
    .sidebar.open {{ transform: translateX(0); }}
    .main {{ margin-left: 0; }}
    .sidebar-toggle {{ display: flex; align-items: center; justify-content: center; }}
    .charts-grid, .charts-grid-3 {{ grid-template-columns: 1fr; }}
  }}
</style>
</head>
<body>

<button class="sidebar-toggle" onclick="toggleSidebar()" title="Menu">☰</button>

<div style="background:#FEF3C7;color:#92400E;text-align:center;padding:6px 16px;font-size:11px;font-weight:600;position:relative;z-index:300">
  ⚠️ Ambiente de teste — atualizado automaticamente via GitHub Actions + Databricks
</div>

<div class="layout">
  <aside class="sidebar" id="sidebar">
    <div class="sidebar-logo">
      <div class="logo-icon">🛡️</div>
      <div class="logo-text">Governança<br>Federada</div>
    </div>
    <nav class="nav-section">
      <div class="nav-label">Principal</div>
      <button class="nav-item active" onclick="goTo('visao-geral', this)"><span class="nav-icon">📊</span> Visão Geral</button>
      <button class="nav-item" onclick="goTo('sistemas', this)"><span class="nav-icon">🗄️</span> Sistemas</button>
      <button class="nav-item" onclick="goTo('retencao', this)"><span class="nav-icon">🗃️</span> Retenção e Expurgo</button>
      <button class="nav-item" onclick="goTo('conformidade', this)"><span class="nav-icon">✅</span> Conformidade</button>
      <div class="nav-label" style="margin-top:10px">Análise</div>
      <button class="nav-item" onclick="goTo('dominios', this)"><span class="nav-icon">🏛️</span> Por Domínio</button>
      <button class="nav-item" onclick="goTo('evolucao', this)"><span class="nav-icon">📈</span> Evolução</button>
    </nav>
    <div class="sidebar-footer">
      Fonte: Databricks SQL (auto)<br><strong style="color:#94A3B8">{gerado_em}</strong>
    </div>
  </aside>

  <div class="main">
    <div class="topbar">
      <div>
        <div class="topbar-title">Indicadores de Governança Federada</div>
        <div class="topbar-sub">Ambiente transacional · {total} sistemas monitorados</div>
      </div>
      <div class="topbar-right">
        <select class="filter-select" id="filterDominio" onchange="filterTable()">
          <option value="">Todos os domínios</option>
          {''.join(f'<option value="{d}">{d}</option>' for d in dominio_labels)}
        </select>
        <select class="filter-select" id="filterStatus" onchange="filterTable()">
          <option value="">Todos os status</option>
          <option value="Conforme">Conforme</option>
          <option value="Parcial">Parcial</option>
          <option value="Não Conforme">Não Conforme</option>
        </select>
        <span class="badge-source">🤖 Auto via Actions</span>
        <span class="badge-update">🔄 {gerado_em}</span>
      </div>
    </div>

    <div class="page active" id="page-visao-geral">
      <div class="kpi-grid">
        <div class="kpi-card">
          <div class="kpi-icon">📊</div>
          <div class="kpi-label">Índice Geral de Governança</div>
          <div class="kpi-value">{score_medio}%</div>
          <div class="kpi-sub {'kpi-green' if score_medio >= 80 else 'kpi-amber' if score_medio >= 60 else 'kpi-red'}">
            {'BOM ↑' if score_medio >= 80 else 'REGULAR ↗' if score_medio >= 60 else 'ATENÇÃO ↓'}
          </div>
        </div>
        <div class="kpi-card">
          <div class="kpi-icon">🗄️</div>
          <div class="kpi-label">Sistemas Monitorados</div>
          <div class="kpi-value">{total}</div>
          <div class="kpi-sub kpi-green">{conformes} conformes</div>
        </div>
        <div class="kpi-card">
          <div class="kpi-icon">👤</div>
          <div class="kpi-label">Com Data Owner</div>
          <div class="kpi-value">{pct_owner}%</div>
          <div class="kpi-sub kpi-blue">{com_owner} de {total}</div>
        </div>
        <div class="kpi-card">
          <div class="kpi-icon">📂</div>
          <div class="kpi-label">Metadados Completos</div>
          <div class="kpi-value">{pct_meta_avg}%</div>
          <div class="kpi-sub {'kpi-green' if pct_meta_avg >= 80 else 'kpi-amber'}">média por sistema</div>
        </div>
        <div class="kpi-card">
          <div class="kpi-icon">🗃️</div>
          <div class="kpi-label">Dentro da Política Ret.</div>
          <div class="kpi-value">{pct_ret_avg}%</div>
          <div class="kpi-sub {'kpi-green' if pct_ret_avg >= 80 else 'kpi-amber'}">média por sistema</div>
        </div>
        <div class="kpi-card">
          <div class="kpi-icon">⚠️</div>
          <div class="kpi-label">Não Conformidades</div>
          <div class="kpi-value">{total_nc}</div>
          <div class="kpi-sub kpi-red">{nao_conf} sistemas críticos</div>
        </div>
      </div>

      <div class="charts-grid-3">
        <div class="chart-card">
          <div class="chart-title">Evolução do Índice Geral <span>Jan–Jun/2024</span></div>
          <canvas id="chartEvo"></canvas>
        </div>
        <div class="chart-card">
          <div class="chart-title">Status de Conformidade</div>
          <canvas id="chartStatus"></canvas>
        </div>
        <div class="chart-card">
          <div class="chart-title">Status de Retenção</div>
          <canvas id="chartRet"></canvas>
        </div>
      </div>

      <div class="chart-card" style="margin-bottom:24px">
        <div class="chart-title">Score por Domínio <span>média dos sistemas</span></div>
        <canvas id="chartDominio" style="max-height:180px"></canvas>
      </div>
    </div>

    <div class="page" id="page-sistemas">
      <div class="table-card">
        <div class="table-header">
          <div class="table-title">📋 Todos os Sistemas — Visão Consolidada</div>
          <input class="search-box" type="text" id="searchInput" placeholder="🔍 Buscar sistema..." oninput="filterTable()">
        </div>
        <div class="table-wrap">
          <table id="mainTable">
            <thead>
              <tr>
                <th>Sistema</th><th>Tipo</th><th>Domínio</th><th>Data Owner</th>
                <th>Score Gov.</th><th>Metadados</th><th>Retenção</th>
                <th>NCs</th><th>Conformidade</th><th>Última Revisão</th>
              </tr>
            </thead>
            <tbody id="tableBody"></tbody>
          </table>
        </div>
        <div class="table-footer" id="tableFooter"></div>
      </div>
    </div>

    <div class="page" id="page-retencao">
      <div class="kpi-grid" style="grid-template-columns: repeat(4,1fr)">
        <div class="kpi-card"><div class="kpi-icon">✅</div><div class="kpi-label">Dentro da Política</div><div class="kpi-value">{ret_dist[0]}</div><div class="kpi-sub kpi-green">sistemas</div></div>
        <div class="kpi-card"><div class="kpi-icon">📦</div><div class="kpi-label">Revisão Pendente</div><div class="kpi-value">{ret_dist[1]}</div><div class="kpi-sub kpi-amber">sistemas</div></div>
        <div class="kpi-card"><div class="kpi-icon">🚨</div><div class="kpi-label">Política Vencida</div><div class="kpi-value">{ret_dist[2]}</div><div class="kpi-sub kpi-red">sistemas</div></div>
        <div class="kpi-card"><div class="kpi-icon">📊</div><div class="kpi-label">Média Conformidade</div><div class="kpi-value">{pct_ret_avg}%</div><div class="kpi-sub kpi-blue">geral</div></div>
      </div>
      <div class="table-card">
        <div class="table-header"><div class="table-title">🗃️ Retenção e Expurgo por Sistema</div></div>
        <div class="table-wrap">
          <table>
            <thead><tr><th>Sistema</th><th>Domínio</th><th>Criticidade</th><th>% Dentro Política</th><th>Status Retenção</th><th>Responsável</th><th>Última Revisão</th></tr></thead>
            <tbody>
              {''.join(f"""
              <tr>
                <td>{s['nome']}</td>
                <td>{s['dominio']}</td>
                <td><span class="badge {'badge-red' if s['criticidade']=='Alta' else 'badge-amber' if s['criticidade']=='Média' else 'badge-gray'}">{s['criticidade']}</span></td>
                <td>
                  <div style="display:flex;align-items:center;gap:8px">
                    <div class="score-bar-wrap"><div class="score-bar-bg"><div class="score-bar-fill" style="width:{s['pct_retencao']}%;background:{'#16A34A' if s['pct_retencao']>80 else '#D97706' if s['pct_retencao']>60 else '#DC2626'}"></div></div></div>
                    <span style="font-size:11px;font-weight:700;color:{'#16A34A' if s['pct_retencao']>80 else '#D97706' if s['pct_retencao']>60 else '#DC2626'}">{s['pct_retencao']}%</span>
                  </div>
                </td>
                <td><span class="badge {'badge-green' if s['status_ret']=='Ativo' else 'badge-amber' if s['status_ret']=='Revisão Pendente' else 'badge-red'}">{s['status_ret']}</span></td>
                <td>{s['owner']}</td>
                <td>{s['data_ultima_revisao']}</td>
              </tr>""" for s in sistemas)}
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <div class="page" id="page-conformidade">
      <div class="kpi-grid" style="grid-template-columns:repeat(3,1fr)">
        <div class="kpi-card"><div class="kpi-icon">✅</div><div class="kpi-label">Conformes</div><div class="kpi-value kpi-green">{conformes}</div><div class="kpi-sub kpi-green">{round(conformes/total*100)}% do total</div></div>
        <div class="kpi-card"><div class="kpi-icon">⚠️</div><div class="kpi-label">Parcialmente Conformes</div><div class="kpi-value kpi-amber">{parciais}</div><div class="kpi-sub kpi-amber">{round(parciais/total*100)}% do total</div></div>
        <div class="kpi-card"><div class="kpi-icon">🚨</div><div class="kpi-label">Não Conformes</div><div class="kpi-value kpi-red">{nao_conf}</div><div class="kpi-sub kpi-red">{round(nao_conf/total*100)}% do total</div></div>
      </div>
      <div class="table-card">
        <div class="table-header"><div class="table-title">✅ Avaliação de Conformidade por Sistema</div></div>
        <div class="table-wrap">
          <table>
            <thead><tr><th>Sistema</th><th>Domínio</th><th>Score Gov.</th><th>NCs Abertas</th><th>Conformidade</th><th>Data Owner</th><th>Data Steward</th></tr></thead>
            <tbody>
              {''.join(f"""
              <tr>
                <td>{s['nome']}</td><td>{s['dominio']}</td>
                <td>
                  <div style="display:flex;align-items:center;gap:8px">
                    <div class="score-bar-wrap"><div class="score-bar-bg"><div class="score-bar-fill" style="width:{s['score']}%;background:{'#16A34A' if s['score']>=80 else '#D97706' if s['score']>=60 else '#DC2626'}"></div></div></div>
                    <strong style="color:{'#16A34A' if s['score']>=80 else '#D97706' if s['score']>=60 else '#DC2626'}">{s['score']}%</strong>
                  </div>
                </td>
                <td><span class="badge {'badge-red' if s['nao_conformidades']>2 else 'badge-amber' if s['nao_conformidades']>0 else 'badge-green'}">{s['nao_conformidades']}</span></td>
                <td><span class="badge {'badge-green' if s['status_gov']=='Conforme' else 'badge-amber' if s['status_gov']=='Parcial' else 'badge-red'}">{s['status_gov']}</span></td>
                <td>{s['owner']}</td><td>{s['steward']}</td>
              </tr>""" for s in sistemas)}
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <div class="page" id="page-dominios">
      <div class="charts-grid" style="margin-bottom:24px">
        <div class="chart-card"><div class="chart-title">Score médio por Domínio</div><canvas id="chartDominioBar"></canvas></div>
        <div class="chart-card"><div class="chart-title">Distribuição de Sistemas por Domínio</div><canvas id="chartDominioPie"></canvas></div>
      </div>
      <div id="dominioCards" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:14px"></div>
    </div>

    <div class="page" id="page-evolucao">
      <div class="chart-card" style="margin-bottom:20px">
        <div class="chart-title">Evolução do Score por Sistema <span>Jan–Jun/2024</span></div>
        <canvas id="chartMultiLine" style="max-height:300px"></canvas>
      </div>
      <div class="table-card">
        <div class="table-header"><div class="table-title">📈 Variação no Período (Jan → Jun)</div></div>
        <div class="table-wrap">
          <table>
            <thead><tr><th>Sistema</th><th>Jan</th><th>Fev</th><th>Mar</th><th>Abr</th><th>Mai</th><th>Jun</th><th>Variação</th></tr></thead>
            <tbody>
              {''.join(f"""
              <tr>
                <td>{s['nome']}</td>
                {''.join(f"<td>{v}%</td>" for v in s['score_hist'])}
                <td><span class="badge {'badge-green' if s['score_hist'][-1]-s['score_hist'][0]>0 else 'badge-red'}">{'↑' if s['score_hist'][-1]-s['score_hist'][0]>0 else '↓'} {abs(s['score_hist'][-1]-s['score_hist'][0])}pp</span></td>
              </tr>""" for s in sistemas)}
            </tbody>
          </table>
        </div>
      </div>
    </div>

  </div>
</div>

<script>
const SISTEMAS   = {sistemas_json};
const MESES      = {meses_json};
const SCORE_EVO  = {score_evo_json};
const DOM_LABELS = {dom_labels_json};
const DOM_SCORES = {dom_scores_json};
const DOM_COUNTS = {dom_counts_json};
const STATUS_DIST = {status_dist_json};
const RET_DIST   = {ret_dist_json};

const COLORS = {{
  green:'#16A34A', amber:'#D97706', red:'#DC2626',
  blue:'#2563EB', cyan:'#0891B2', purple:'#7C3AED',
  gray:'#94A3B8', navy:'#0F1E35',
  lblueA:'rgba(37,99,235,0.15)',
}};
const PALETTE = ['#2563EB','#0891B2','#16A34A','#D97706','#7C3AED','#DC2626','#0F766E','#9333EA'];

function goTo(pageId, btn) {{
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  document.getElementById('page-' + pageId).classList.add('active');
  if (btn) btn.classList.add('active');
  if (window.innerWidth <= 768) document.getElementById('sidebar').classList.remove('open');
  if (pageId === 'dominios') renderDominioCards();
}}
function toggleSidebar() {{ document.getElementById('sidebar').classList.toggle('open'); }}

function scoreColor(v) {{ return v >= 80 ? COLORS.green : v >= 60 ? COLORS.amber : COLORS.red; }}
function statusBadge(s) {{
  const map = {{'Conforme':'badge-green','Parcial':'badge-amber','Não Conforme':'badge-red'}};
  return `<span class="badge ${{map[s]||'badge-gray'}}">${{s}}</span>`;
}}

function renderTable(data) {{
  const tbody = document.getElementById('tableBody');
  const footer = document.getElementById('tableFooter');
  tbody.innerHTML = data.map(s => `
    <tr>
      <td>${{s.nome}}</td>
      <td><span class="badge badge-blue">${{s.tipo}}</span></td>
      <td>${{s.dominio}}</td>
      <td>${{s.owner}}</td>
      <td>
        <div style="display:flex;align-items:center;gap:8px">
          <div class="score-bar-wrap"><div class="score-bar-bg">
            <div class="score-bar-fill" style="width:${{s.score}}%;background:${{scoreColor(s.score)}}"></div>
          </div></div>
          <strong style="color:${{scoreColor(s.score)}}">${{s.score}}%</strong>
        </div>
      </td>
      <td>${{s.pct_metadados}}%</td>
      <td>${{s.pct_retencao}}%</td>
      <td><span class="badge ${{s.nao_conformidades>2?'badge-red':s.nao_conformidades>0?'badge-amber':'badge-green'}}">${{s.nao_conformidades}}</span></td>
      <td>${{statusBadge(s.status_gov)}}</td>
      <td>${{s.data_ultima_revisao}}</td>
    </tr>`).join('');
  footer.textContent = `Exibindo ${{data.length}} de ${{SISTEMAS.length}} sistemas`;
}}

function filterTable() {{
  const search = (document.getElementById('searchInput')?.value || '').toLowerCase();
  const dominio = document.getElementById('filterDominio').value;
  const status  = document.getElementById('filterStatus').value;
  const filtered = SISTEMAS.filter(s =>
    (!search  || s.nome.toLowerCase().includes(search) || s.id.toLowerCase().includes(search)) &&
    (!dominio || s.dominio === dominio) &&
    (!status  || s.status_gov === status)
  );
  renderTable(filtered);
}}

function renderDominioCards() {{
  const container = document.getElementById('dominioCards');
  if (container.innerHTML) return;
  const groups = {{}};
  SISTEMAS.forEach(s => {{ (groups[s.dominio] = groups[s.dominio]||[]).push(s); }});
  container.innerHTML = Object.entries(groups).map(([dom, sis]) => {{
    const avg = Math.round(sis.reduce((a,s)=>a+s.score,0)/sis.length);
    const c = avg>=80?COLORS.green:avg>=60?COLORS.amber:COLORS.red;
    return `<div style="background:#fff;border-radius:12px;padding:18px 20px;border:1px solid #E2E8F0;box-shadow:0 1px 4px rgba(0,0,0,0.05)">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
        <strong style="font-size:13px;color:#0F1E35">${{dom}}</strong>
        <span style="font-size:22px;font-weight:800;color:${{c}}">${{avg}}%</span>
      </div>
      <div style="font-size:11px;color:#64748B;margin-bottom:10px">${{sis.length}} sistemas</div>
      ${{sis.map(s=>`<div style="display:flex;justify-content:space-between;align-items:center;padding:5px 0;border-top:1px solid #F1F5F9;font-size:11px">
        <span style="color:#334155">${{s.nome.length>22?s.nome.slice(0,22)+'…':s.nome}}</span>
        <span style="font-weight:700;color:${{scoreColor(s.score)}}">${{s.score}}%</span>
      </div>`).join('')}}
    </div>`;
  }}).join('');
}}

const chartDefaults = {{
  plugins: {{ legend: {{ labels: {{ font: {{ family:"'Segoe UI',system-ui" }}, size:11 }} }} }},
  animation: {{ duration: 600 }},
}};

new Chart(document.getElementById('chartEvo'), {{
  type: 'line',
  data: {{ labels: MESES, datasets: [{{ label:'Índice Geral', data: SCORE_EVO, borderColor: COLORS.blue,
    backgroundColor: COLORS.lblueA, fill:true, tension:0.4, pointRadius:4, pointBackgroundColor: COLORS.blue }}] }},
  options: {{ ...chartDefaults, scales: {{
    y: {{ min:30, max:100, ticks:{{callback:v=>v+'%',font:{{size:10}}}}, grid:{{color:'#F1F5F9'}} }},
    x: {{ ticks:{{font:{{size:10}}}}, grid:{{display:false}} }} }} }}
}});

new Chart(document.getElementById('chartStatus'), {{
  type: 'doughnut',
  data: {{ labels: ['Conforme','Parcial','Não Conforme'], datasets: [{{ data: STATUS_DIST,
    backgroundColor:[COLORS.green, COLORS.amber, COLORS.red], borderWidth:2, borderColor:'#fff' }}] }},
  options: {{ ...chartDefaults, cutout:'65%' }}
}});

new Chart(document.getElementById('chartRet'), {{
  type: 'doughnut',
  data: {{ labels: ['Ativo','Revisão Pendente','Vencido'], datasets: [{{ data: RET_DIST,
    backgroundColor:[COLORS.green, COLORS.amber, COLORS.red], borderWidth:2, borderColor:'#fff' }}] }},
  options: {{ ...chartDefaults, cutout:'65%' }}
}});

new Chart(document.getElementById('chartDominio'), {{
  type: 'bar',
  data: {{ labels: DOM_LABELS, datasets: [{{ label:'Score médio', data: DOM_SCORES,
    backgroundColor: DOM_SCORES.map(v=>v>=80?COLORS.green:v>=60?COLORS.amber:COLORS.red), borderRadius:6 }}] }},
  options: {{ ...chartDefaults, indexAxis:'y',
    scales: {{ x:{{min:0,max:100,ticks:{{callback:v=>v+'%',font:{{size:10}}}},grid:{{color:'#F1F5F9'}}}},
              y:{{ticks:{{font:{{size:10}}}},grid:{{display:false}}}} }},
    plugins:{{ ...chartDefaults.plugins, legend:{{display:false}} }} }}
}});

new Chart(document.getElementById('chartDominioBar'), {{
  type: 'bar',
  data: {{ labels: DOM_LABELS, datasets: [{{ label:'Score médio', data: DOM_SCORES,
    backgroundColor: PALETTE, borderRadius:6 }}] }},
  options: {{ ...chartDefaults, scales: {{
    y:{{min:0,max:100,ticks:{{callback:v=>v+'%',font:{{size:10}}}},grid:{{color:'#F1F5F9'}}}},
    x:{{ticks:{{font:{{size:10}}}},grid:{{display:false}}}} }},
    plugins:{{...chartDefaults.plugins,legend:{{display:false}}}} }}
}});

new Chart(document.getElementById('chartDominioPie'), {{
  type: 'pie',
  data: {{ labels: DOM_LABELS, datasets: [{{ data: DOM_COUNTS, backgroundColor: PALETTE, borderWidth:2, borderColor:'#fff' }}] }},
  options: {{ ...chartDefaults }}
}});

new Chart(document.getElementById('chartMultiLine'), {{
  type: 'line',
  data: {{ labels: MESES, datasets: SISTEMAS.map((s,i) => ({{
      label: s.nome.length>20?s.nome.slice(0,20)+'…':s.nome,
      data: s.score_hist, borderColor: PALETTE[i % PALETTE.length],
      backgroundColor: 'transparent', tension: 0.4, pointRadius: 3, }})) }},
  options: {{ ...chartDefaults, scales: {{
      y:{{min:20,max:100,ticks:{{callback:v=>v+'%',font:{{size:10}}}},grid:{{color:'#F1F5F9'}}}},
      x:{{ticks:{{font:{{size:10}}}},grid:{{display:false}}}} }} }}
}});

renderTable(SISTEMAS);
</script>
</body>
</html>"""
    return html


# ── 4. Execução principal ─────────────────────────────────────────────────────
def main():
    result = run_query(SQL_QUERY)
    sistemas = parse_result_to_dicts(result)
    print(f"📊 {len(sistemas)} sistemas recebidos do Databricks")

    html = build_html(sistemas)

    output_path = "index.html"  # gera na raiz do repo
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"✅ {output_path} atualizado com sucesso")
    print(f"   Score médio: {round(sum(s['score'] for s in sistemas) / len(sistemas), 1)}%")


if __name__ == "__main__":
    main()
