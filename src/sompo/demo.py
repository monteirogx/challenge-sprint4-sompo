"""Demonstração guiada da API: simula sensores em campo enviando leituras.

Cenários: leitura normal, máquina em degradação, sensor com campos ausentes,
leitura inválida, reenvio duplicado, chave inválida, perfil sem permissão,
lote misto e verificação de integridade. Salva a transcrição em
docs/evidencias/demo_api.json (evidência de validação da integração).
"""
import json
import os
from datetime import datetime, timedelta

import httpx
import numpy as np

from sompo import config
from sompo.simulador import gerar_leitura_ao_vivo


def _chave(perfil: str) -> str:
    return config.chaves_api().get(perfil, "")


def executar(url_base: str = "http://localhost:8000", cliente: httpx.Client | None = None) -> list[dict]:
    rng = np.random.default_rng(int(datetime.now().timestamp()))
    cliente = cliente or httpx.Client(base_url=url_base, timeout=30)
    agora = datetime.now().replace(microsecond=0)
    transcricao = []

    def chamar(titulo, metodo, rota, perfil=None, corpo=None, chave=None):
        cabecalhos = {"X-API-Key": chave if chave is not None else _chave(perfil)} if (perfil or chave is not None) else {}
        resposta = cliente.request(metodo, rota, json=corpo, headers=cabecalhos)
        conteudo = resposta.json()
        transcricao.append({"cenario": titulo, "rota": f"{metodo} {rota}", "perfil": perfil,
                            "status_http": resposta.status_code, "resposta": conteudo})
        resumo = (f"score={conteudo.get('score_risco')} nível={conteudo.get('nivel_risco')}"
                  if isinstance(conteudo, dict) and "score_risco" in conteudo
                  else json.dumps(conteudo, ensure_ascii=False)[:150])
        print(f"\n▶ {titulo}\n  {metodo} {rota} [{perfil or 'sem chave'}] -> HTTP {resposta.status_code}\n  {resumo}")
        return conteudo

    chamar("1. Saúde da API", "GET", "/saude")

    normal = gerar_leitura_ao_vivo(rng, "EQ-201", "Trator", "PR", 3)
    normal["data_hora"] = str(agora - timedelta(minutes=30))
    chamar("2. Trator novo em operação normal", "POST", "/telemetria", "operador", normal)

    critica = gerar_leitura_ao_vivo(rng, "EQ-202", "Colheitadeira", "MT", 16, estressada=True)
    critica["data_hora"] = str(agora - timedelta(minutes=20))
    critica["operador_id"] = "MAT-99871"
    r = chamar("3. Colheitadeira antiga em degradação (deve alertar)", "POST", "/telemetria", "operador", critica)
    if "recomendacoes" in r:
        print("  Fatores:", [f["nome"] for f in r["fatores"]["principais_contribuicoes_modelo"]])
        print("  Operador:", r["recomendacoes"]["operador"])
        print("  Técnico :", r["recomendacoes"]["tecnico"])

    incompleta = gerar_leitura_ao_vivo(rng, "EQ-203", "Pulverizador", "GO", 8)
    incompleta.update({"data_hora": str(agora - timedelta(minutes=10)), "pressao_oleo_psi": None, "umidade_relativa_pct": None})
    chamar("4. Sensor com 2 campos ausentes (completa e sinaliza)", "POST", "/telemetria", "tecnico", incompleta)

    invalida = dict(normal, id_equipamento="EQ-204", temperatura_motor_c=-999, regiao="XX",
                    data_hora=str(agora - timedelta(minutes=5)))
    chamar("5. Leitura inválida (erro de sensor + região inexistente)", "POST", "/telemetria", "operador", invalida)

    chamar("6. Reenvio duplicado da mesma leitura", "POST", "/telemetria", "operador", normal)
    chamar("7. Chave de API inválida", "POST", "/telemetria", corpo=normal, chave="chave-errada")
    chamar("8. Perfil gestor tentando enviar telemetria (sem permissão)", "POST", "/telemetria", "gestor", normal)

    lote = [dict(gerar_leitura_ao_vivo(rng, f"EQ-21{i}", "Trator", "RS", 5 + i), data_hora=str(agora - timedelta(minutes=40 + i)))
            for i in range(4)]
    lote.append({"id_equipamento": "abc"})
    chamar("9. Lote com 4 leituras válidas e 1 inválida", "POST", "/telemetria/lote", "tecnico", lote)

    chamar("10. Alertas ativos na frota (gestor)", "GET", "/alertas", "gestor")
    if r.get("id_alerta"):
        chamar(f"11. Técnico reconhece o alerta #{r['id_alerta']} (inspeção agendada)", "PATCH", f"/alertas/{r['id_alerta']}",
               "tecnico", {"status": "RECONHECIDO", "observacao": "Inspeção de rolamentos agendada para amanhã"})
    chamar("12. Resumo de risco por região (gestor)", "GET", "/relatorios/resumo?agrupar_por=regiao", "gestor")
    chamar("13. Operador tentando ver auditoria (sem permissão)", "GET", "/auditoria?limite=5", "operador")
    chamar("14. Últimos eventos de auditoria (analista)", "GET", "/auditoria?limite=5", "analista")
    chamar("15. Verificação de integridade (analista)", "GET", "/auditoria/integridade", "analista")

    config.DIR_EVIDENCIAS.mkdir(parents=True, exist_ok=True)
    (config.DIR_EVIDENCIAS / "demo_api.json").write_text(
        json.dumps(transcricao, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nTranscrição salva em {config.DIR_EVIDENCIAS / 'demo_api.json'}")
    return transcricao


if __name__ == "__main__":
    executar(os.getenv("SOMPO_API_URL", "http://localhost:8000"))
