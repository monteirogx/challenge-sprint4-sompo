"""Ponto de entrada único do MVP Sompo Seguros (Sprint 4).

Uso:
    python main.py pipeline     # coleta -> tratamento -> banco -> modelo -> scores -> relatório
    python main.py api          # sobe a API REST (http://localhost:8000/docs)
    python main.py dashboard    # sobe o dashboard (http://localhost:8501)
    python main.py demo         # envia leituras de demonstração para a API em execução
    python main.py relatorio    # regenera o relatório HTML e os gráficos
    python main.py verificar    # confere integridade do banco, da auditoria e do modelo
    python main.py testes       # roda a suíte de testes automatizados
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
sys.path.insert(0, str(RAIZ / "src"))


def main() -> int:
    parser = argparse.ArgumentParser(description="MVP Sompo Seguros — prevenção de quebra de maquinário")
    parser.add_argument("comando", choices=["pipeline", "api", "dashboard", "demo", "relatorio", "verificar", "testes"])
    parser.add_argument("--equipamentos", type=int, default=80, help="tamanho da frota simulada (pipeline)")
    parser.add_argument("--dias", type=int, default=150, help="dias de histórico simulado (pipeline)")
    parser.add_argument("--porta", type=int, default=8000, help="porta da API")
    args = parser.parse_args()

    if args.comando == "pipeline":
        from sompo import pipeline
        resumo = pipeline.executar(args.equipamentos, args.dias)
        print(json.dumps(resumo, indent=2, ensure_ascii=False, default=str))
    elif args.comando == "api":
        import uvicorn
        uvicorn.run("sompo.api:app", host="127.0.0.1", port=args.porta, app_dir=str(RAIZ / "src"))
    elif args.comando == "dashboard":
        return subprocess.call([sys.executable, "-m", "streamlit", "run", str(RAIZ / "dashboard" / "app.py")])
    elif args.comando == "demo":
        from sompo import demo
        demo.executar(f"http://127.0.0.1:{args.porta}")
    elif args.comando == "relatorio":
        from sompo import relatorios
        print("Relatório gerado em:", relatorios.gerar_relatorio())
    elif args.comando == "verificar":
        from sompo import banco, modelo
        resultado = {"leituras": banco.verificar_integridade_leituras(),
                     "auditoria": banco.verificar_integridade_auditoria()}
        try:
            resultado["modelo"] = {"integro": True, "versao": modelo.carregar()["versao"]}
        except modelo.ModeloIndisponivel as erro:
            resultado["modelo"] = {"integro": False, "erro": str(erro)}
        print(json.dumps(resultado, indent=2, ensure_ascii=False))
        return 0 if all(v.get("integra", v.get("integro")) for v in resultado.values()) else 1
    elif args.comando == "testes":
        return subprocess.call([sys.executable, "-m", "pytest", "-v"], cwd=RAIZ)
    return 0


if __name__ == "__main__":
    sys.exit(main())
