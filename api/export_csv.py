import os
import db_local


def main():
    db_local.init_db()
    csv_text = db_local.export_csv()
    saida = os.path.join(os.path.dirname(__file__), "..", "historico_cyborg.csv")
    with open(saida, "w", encoding="utf-8-sig", newline="") as f:
        f.write(csv_text)
    linhas = max(0, csv_text.count("\n") - 1)
    print(f"CSV gerado em: {os.path.abspath(saida)}")
    print(f"Conversas (linhas): {linhas}")


if __name__ == "__main__":
    main()
