import os
from pypdf import PdfReader
from langchain_huggingface import HuggingFaceEmbeddings

import db_local

EMBED_MODEL = os.getenv("EMBED_MODEL", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
embed_model = HuggingFaceEmbeddings(model_name=EMBED_MODEL)


def split_text(texto, chunk_size=1000, overlap=150):
    """Fatiador recursivo simples: pedaços de ~chunk_size caracteres com
    sobreposição, tentando cortar em quebras naturais (parágrafo/linha/espaço)."""
    texto = (texto or "").strip()
    n = len(texto)
    if n == 0:
        return []
    chunks = []
    start = 0
    while start < n:
        end = min(start + chunk_size, n)
        if end < n:
            janela = texto[start:end]
            corte = -1
            for sep in ("\n\n", "\n", " "):
                idx = janela.rfind(sep)
                if idx > chunk_size * 0.5:
                    corte = idx
                    break
            if corte != -1:
                end = start + corte
        pedaco = texto[start:end].strip()
        if pedaco:
            chunks.append(pedaco)
        if end >= n:
            break
        start = max(end - overlap, start + 1)
    return chunks


def processar_pdfs():
    db_local.init_db()
    db_local.clear_documents()

    pdf_path = os.path.join(os.path.dirname(__file__), "documentos_pdf")
    if not os.path.exists(pdf_path):
        print(f"Erro: a pasta {pdf_path} não foi encontrada.")
        return

    total = 0
    for arquivo in sorted(os.listdir(pdf_path)):
        if not arquivo.endswith(".pdf"):
            continue
        try:
            print(f"\n--- Iniciando: {arquivo} ---")
            reader = PdfReader(os.path.join(pdf_path, arquivo))

            texto_completo = ""
            for page in reader.pages:
                try:
                    extraido = page.extract_text()
                    if extraido:
                        texto_completo += extraido + "\n"
                except Exception:
                    continue

            if not texto_completo.strip():
                print(f"  {arquivo} ignorado: sem texto extraível.")
                continue

            chunks = split_text(texto_completo, chunk_size=800, overlap=120)
            print(f"  {len(chunks)} trechos.")

            for i, chunk_text in enumerate(chunks):
                embedding = embed_model.embed_query(chunk_text)
                db_local.add_document(f"{arquivo}_parte_{i+1}", chunk_text, embedding)
                total += 1
                if (i + 1) % 20 == 0:
                    print(f"    ... {i+1} trechos gravados")

            print(f"  OK: {arquivo}")
        except Exception as e:
            print(f"  Erro em {arquivo}: {e}")

    print(f"\nConcluido. Total de trechos no banco: {db_local.count_documents()} (adicionados agora: {total})")


if __name__ == "__main__":
    print("Iniciando ingestao de conhecimento do Cyborg AI (SQLite local)...")
    processar_pdfs()
