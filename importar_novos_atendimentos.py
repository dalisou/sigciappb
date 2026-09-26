"""Filtra novos registros da planilha CIAP por CPF e número do processo."""

from __future__ import annotations

import argparse
import os
import re
import unicodedata
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import create_engine, text


ROOT = Path(__file__).resolve().parent
DEFAULT_EXCEL = Path.home() / "Downloads" / "CIAP" / "BASE_ATENDIMENTOS_CIAP_2026_COMPLETA.xlsx"
DEFAULT_CSV = Path.home() / "Downloads" / "ciap_atendimentos (1).csv"
DEFAULT_OUTPUT = ROOT / "novos_atendimentos_importados.csv"
SHEET_NAME = "Dash_Base_Dados"
HEADER_ROW = 1
DEFAULT_START_ROW = 360  # Número da linha física do Excel, contando desde 1.

FIELDS = [
    "nome", "nome_social", "cpf", "rg", "data_nascimento", "nome_mae", "processo", "vara", "rji",
    "telefone", "data_atendimento", "raca", "sexo", "identidade_genero", "orientacao_sexual",
    "escolaridade", "pcd", "tipo_deficiencia", "nacionalidade", "pais", "ocupacao", "profissao",
    "religiao", "diploma_legal", "artigo", "tipo_penal", "grupamento_penal", "natureza_atendimento",
    "medida", "grupo_responsabilizacao", "status", "atendimento_individual", "comparecimento",
    "termino_medida", "situacao_final", "observacoes", "prestacao_servico_comunitario",
    "local_prestacao_servico", "grupo_reflexivo", "tipo_grupo_reflexivo",
]

# Cada campo aceita o nome original da planilha, do CSV ou da coluna SQL.
FIELD_LABELS = {
    "nome": ("Nome do Assistido(a)", "nome"),
    "nome_social": ("Nome Social", "nome_social"),
    "cpf": ("CPF", "cpf"),
    "rg": ("RG/órgão emissor", "rg"),
    "data_nascimento": ("Data de nascimento", "data_nascimento"),
    "nome_mae": ("Nome da mãe", "nome_mae"),
    "processo": ("Número do Processo", "processo"),
    "vara": ("Órgão Judicial/Vara", "vara"),
    "rji": ("Nº Inscrição (ID único)", "Nº  Inscrição (ID único)", "rji"),
    "telefone": ("Telefone(s) WhatsApp", "telefone"),
    "data_atendimento": ("Data de Atendimento", "data_atendimento"),
    "raca": ("Raça/cor da pele (Declarada)", "raca"),
    "sexo": ("Sexo (biológico)", "sexo"),
    "identidade_genero": ("Identidade de Gênero (Declarada)", "identidade_genero"),
    "orientacao_sexual": ("Orientação Sexual (autodeclaração)", "orientacao_sexual"),
    "escolaridade": ("Escolaridade", "escolaridade"),
    "pcd": ("Pessoa com Deficiência", "pcd"),
    "tipo_deficiencia": ("Tipo de Deficiência", "tipo_deficiencia"),
    "nacionalidade": ("Nacionalidade", "nacionalidade"),
    "pais": ("País", "pais"),
    "ocupacao": ("Ocupação", "ocupacao"),
    "profissao": ("Profissão", "profissao"),
    "religiao": ("Religião", "religiao"),
    "diploma_legal": ("Diploma Legal", "diploma_legal"),
    "artigo": ("Artigo/Capitulação", "artigo"),
    "tipo_penal": ("Tipo Penal", "tipo_penal"),
    "grupamento_penal": ("Grupamento Penal", "grupamento_penal"),
    "natureza_atendimento": ("Natureza do Atendimento", "natureza_atendimento"),
    "medida": ("Tipo de Medida/Alternativa", "medida"),
    "grupo_responsabilizacao": ("Grupo de Responsabilização", "grupo_responsabilizacao"),
    "status": ("Status do Atendimento", "status"),
    "atendimento_individual": (
        "Atendimento Individual (Assistente Social, Psicólogo, Jurídico)",
        "Atendimento Individual", "atendimento_individual",
    ),
    "comparecimento": ("Comparecimento Voluntário", "comparecimento"),
    "termino_medida": ("Data de Término da Medida", "termino_medida"),
    "situacao_final": ("Situação Final", "situacao_final"),
    "observacoes": ("Observação/Justificativas", "observacoes"),
    "prestacao_servico_comunitario": ("Prestação de Serviço Comunitário", "prestacao_servico_comunitario"),
    "local_prestacao_servico": ("Local da Prestação de Serviço", "local_prestacao_servico"),
    "grupo_reflexivo": ("Grupo Reflexivo", "grupo_reflexivo"),
    "tipo_grupo_reflexivo": ("Tipo de Grupo Reflexivo", "tipo_grupo_reflexivo"),
}


def normalize_header(value: Any) -> str:
    """Normaliza rótulos para comparar cabeçalhos com acentos/ espaços diferentes."""
    text_value = unicodedata.normalize("NFKD", str(value or ""))
    text_value = "".join(char for char in text_value if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", text_value).strip().casefold()


def normalize_identifier(value: Any) -> str:
    """Mantém somente letras e números para comparar CPF/processo sem pontuação."""
    if value is None or pd.isna(value):
        return ""
    if isinstance(value, (int, float)) and float(value).is_integer():
        value = str(int(value))
    return re.sub(r"[^A-Z0-9]", "", str(value).strip().upper())


def clean_cell(value: Any) -> str:
    """Converte valores Excel para texto limpo sem transformar vazios em 'nan'."""
    if value is None or pd.isna(value):
        return ""
    if isinstance(value, (datetime, date)):
        return value.date().isoformat() if isinstance(value, datetime) else value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def build_header_map(columns: list[Any]) -> dict[str, str]:
    """Retorna campo SQL -> nome exato da coluna na fonte."""
    normalized_columns = {normalize_header(column): str(column) for column in columns}
    result = {}
    for field, aliases in FIELD_LABELS.items():
        for alias in aliases:
            source_column = normalized_columns.get(normalize_header(alias))
            if source_column:
                result[field] = source_column
                break
    return result


def clean_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Limpa células e remove espaços extras de todos os campos textuais."""
    cleaned = frame.copy()
    for column in cleaned.columns:
        cleaned[column] = cleaned[column].map(clean_cell).astype("string").str.strip()
    return cleaned.fillna("")


def map_records(frame: pd.DataFrame, source_label: str) -> list[dict[str, str]]:
    header_map = build_header_map(list(frame.columns))
    missing = [field for field in ("nome", "cpf", "processo") if field not in header_map]
    if missing:
        raise ValueError(f"Colunas obrigatórias ausentes em {source_label}: {', '.join(missing)}")

    mapped_records = []
    for _, row in frame.iterrows():
        record = {field: "" for field in FIELDS}
        for field, source_column in header_map.items():
            record[field] = clean_cell(row[source_column])
        if record["nome"]:
            mapped_records.append(record)
    return mapped_records


def make_key(record: dict[str, Any]) -> tuple[str, str] | None:
    cpf = normalize_identifier(record.get("cpf", ""))
    process = normalize_identifier(record.get("processo", ""))
    if not cpf or not process:
        return None
    return cpf, process


def read_csv_base(path: Path) -> tuple[pd.DataFrame, set[tuple[str, str]]]:
    base = pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    base = clean_frame(base)
    keys = set()
    for record in map_records(base, str(path)):
        key = make_key(record)
        if key:
            keys.add(key)
    return base, keys


def read_excel_candidates(path: Path, sheet_name: str, start_row: int) -> list[dict[str, str]]:
    source = pd.read_excel(path, sheet_name=sheet_name, header=HEADER_ROW, dtype=object)
    source = clean_frame(source)
    # Com header=1, o índice 0 representa a linha física 3 do Excel.
    first_data_index = max(start_row - (HEADER_ROW + 2), 0)
    source = source.iloc[first_data_index:]
    return map_records(source, f"{path} (aba {sheet_name})")


def filter_new_records(
    records: list[dict[str, str]], existing_keys: set[tuple[str, str]]
) -> tuple[list[dict[str, str]], int, int]:
    new_records = []
    duplicate_count = 0
    missing_key_count = 0
    seen = set(existing_keys)
    for record in records:
        key = make_key(record)
        if key is None:
            missing_key_count += 1
        elif key in seen:
            duplicate_count += 1
        else:
            seen.add(key)
            new_records.append(record)
    return new_records, duplicate_count, missing_key_count


def database_url(override: str | None) -> str:
    url = override or os.environ.get("DATABASE_URL")
    if not url:
        url = f"sqlite:///{(ROOT / 'data' / 'ciap.db').as_posix()}"
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url


def database_keys(connection: Any) -> set[tuple[str, str]]:
    rows = connection.execute(text("SELECT cpf, processo FROM pessoas")).mappings()
    return {
        key for row in rows
        if (key := make_key(row)) is not None
    }


def insert_records(connection: Any, records: list[dict[str, str]]) -> None:
    if not records:
        return
    columns = ["criado_em", "criado_por", *FIELDS]
    column_sql = ", ".join(f'"{column}"' for column in columns)
    values_sql = ", ".join(f":{column}" for column in columns)
    statement = text(f"INSERT INTO pessoas ({column_sql}) VALUES ({values_sql})")
    created_at = datetime.now().isoformat(timespec="seconds")
    connection.execute(
        statement,
        [{"criado_em": created_at, "criado_por": None, **record} for record in records],
    )


def write_output(records: list[dict[str, str]], output: Path, csv_columns: list[str] | None) -> None:
    if csv_columns is None:
        frame = pd.DataFrame(records, columns=FIELDS)
    else:
        output_map = build_header_map(csv_columns)
        reverse_map = {field: column for field, column in output_map.items()}
        frame = pd.DataFrame(
            [{column: record.get(field, "") for field, column in reverse_map.items()} for record in records],
            columns=csv_columns,
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(output, index=False, encoding="utf-8-sig")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Identifica novos registros do Excel usando CPF + Número do Processo."
    )
    parser.add_argument("--excel", type=Path, default=DEFAULT_EXCEL, help="Planilha de origem.")
    parser.add_argument("--csv-base", type=Path, default=DEFAULT_CSV, help="CSV de referência.")
    parser.add_argument("--aba", default=SHEET_NAME, help="Nome da aba Excel.")
    parser.add_argument("--linha-inicial", type=int, default=DEFAULT_START_ROW, help="Linha física inicial do Excel.")
    parser.add_argument("--saida", type=Path, default=DEFAULT_OUTPUT, help="CSV de saída dos novos registros.")
    parser.add_argument("--insert-db", action="store_true", help="Insere também na tabela pessoas do banco.")
    parser.add_argument("--database-url", help="URL SQLAlchemy; por padrão usa DATABASE_URL ou o SQLite do app.")
    parser.add_argument("--sem-csv-base", action="store_true", help="Não usa o CSV como referência de duplicidade.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.excel.is_file():
        raise FileNotFoundError(f"Planilha Excel não encontrada: {args.excel}")
    if args.linha_inicial < HEADER_ROW + 2:
        raise ValueError("A linha inicial deve estar depois das linhas de cabeçalho.")

    csv_base = None
    existing_keys: set[tuple[str, str]] = set()
    if not args.sem_csv_base:
        if not args.csv_base.is_file():
            raise FileNotFoundError(f"CSV de referência não encontrado: {args.csv_base}")
        csv_base, existing_keys = read_csv_base(args.csv_base)

    source_records = read_excel_candidates(args.excel, args.aba, args.linha_inicial)
    duplicate_count = missing_key_count = 0

    if args.insert_db:
        engine = create_engine(database_url(args.database_url))
        try:
            with engine.begin() as connection:
                existing_keys.update(database_keys(connection))
                new_records, duplicate_count, missing_key_count = filter_new_records(
                    source_records, existing_keys
                )
                insert_records(connection, new_records)
        finally:
            engine.dispose()
    else:
        new_records, duplicate_count, missing_key_count = filter_new_records(
            source_records, existing_keys
        )

    write_output(new_records, args.saida, list(csv_base.columns) if csv_base is not None else None)
    print(f"Novos registros encontrados: {len(new_records)}")
    print(f"Ignorados por duplicidade (CPF + processo): {duplicate_count}")
    print(f"Sem CPF ou processo e ignorados: {missing_key_count}")
    print(f"Prontos para inserção: {len(new_records)}")
    if args.insert_db:
        print(f"Inseridos no banco: {len(new_records)}")
    print(f"CSV gerado: {args.saida.resolve()}")


if __name__ == "__main__":
    main()