import os, sqlite3, psycopg2

db_path = None
for root, dirs, files in os.walk('.'):
    for f in files:
        if f.endswith(('.db', '.sqlite')):
            p = os.path.join(root, f)
            try:
                c = sqlite3.connect(p)
                tables = [row[0] for row in c.cursor().execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
                c.close()
                if 'pessoas' in tables:
                    db_path = p
                    break
            except Exception:
                pass
    if db_path:
        break

if not db_path:
    print(">>> ATENÇÃO: Nenhum arquivo .db local com a tabela 'pessoas' foi encontrado nesta pasta.")
    print(">>> Copie o arquivo ciap.db (ou pasta instance) da versão antiga do projeto para esta pasta sigciappb-15.09.")
else:
    print(f">>> Banco SQLite localizado em: {db_path}")
    conn_sq = sqlite3.connect(db_path)
    cur_sq = conn_sq.cursor()
    
    conn_pg = psycopg2.connect('postgresql://sigciap_bd_user:ltdlmpcIz4riwLAExB4yDpGDwozYZtUr@dpg-dag7cbdbedkc73folo2g-a.oregon-postgres.render.com/sigciap_bd')
    cur_pg = conn_pg.cursor()
    
    cur_sq.execute('SELECT nome, cpf, rg, mae, pai, data_nascimento, estado_civil, profissao, telefone, celular, endereco, bairro, cidade, estado, cep, numero_processo, vara_comarca, observacoes, data_cadastro, documentos FROM pessoas')
    rows = cur_sq.fetchall()
    
    cur_pg.execute('SELECT LOWER(nome) FROM pessoas WHERE nome IS NOT NULL')
    pg_nomes = {r[0].strip() for r in cur_pg.fetchall()}
    
    inseridos = 0
    for r in rows:
        nome = r[0]
        if nome and nome.strip().lower() not in pg_nomes:
            cur_pg.execute('''
                INSERT INTO pessoas (nome, cpf, rg, mae, pai, data_nascimento, estado_civil, profissao, telefone, celular, endereco, bairro, cidade, estado, cep, numero_processo, vara_comarca, observacoes, data_cadastro, documentos)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ''', r)
            pg_nomes.add(nome.strip().lower())
            inseridos += 1
            print(f" + Inserido: {nome}")
            
    conn_pg.commit()
    cur_sq.close()
    conn_sq.close()
    cur_pg.close()
    conn_pg.close()
    print(f"\n>>> SINCRONIZAÇÃO CONCLUÍDA! {inseridos} assistido(s) importado(s) com sucesso.")
