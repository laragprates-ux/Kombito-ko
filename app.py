import datetime
import logging
import math
import libsql_client
import pandas as pd
import plotly.express as px
import streamlit as st

st.set_page_config(
    page_title="Kombito - Gestão e Finanças", page_icon="🍲", layout="wide"
)

st.title("Kombito")

# Função para conectar ao Turso usando os segredos
def get_turso_client():
  url = st.secrets["TURSO_DATABASE_URL"]
  token = st.secrets["TURSO_AUTH_TOKEN"]
  return libsql_client.create_client_sync(url=url, auth_token=token)


UNIDADES = {
    "kg": ("massa", 1000),
    "g": ("massa", 1),
    "mg": ("massa", 0.001),
    "litro": ("volume", 1000),
    "ml": ("volume", 1),
    "cl": ("volume", 10),
    "dl": ("volume", 100),
    "xícara (240 ml)": ("volume", 240),
    "colher de sopa (15 ml)": ("volume", 15),
    "colher de chá (5 ml)": ("volume", 5),
    "un": ("contagem", 1),
    "dúzia": ("contagem", 12),
    "meia dúzia": ("contagem", 6),
}

UNIDADES_PREPARO = {"massa": "g", "volume": "ml", "contagem": "un"}

CONSULTA_CUSTOS = """
    SELECT f.marmita_id, SUM(f.quantidade * i.preco_unitario) AS custo
    FROM ficha_tecnica AS f
    LEFT JOIN insumos AS i ON i.id = f.insumo_id
    GROUP BY f.marmita_id
    HAVING COUNT(*) = COUNT(i.id)
"""


def executar(sql, parametros=()):
  with get_turso_client() as client:
    return client.execute(sql, parametros)


def embalagem_valida(quantidade, preco):
  return (
      quantidade is not None and math.isfinite(quantidade) and quantidade > 0
      and preco is not None and math.isfinite(preco) and preco >= 0
      and math.isfinite(preco / quantidade)
  )


def migrar_banco(client):
  client.batch([
      """CREATE TABLE IF NOT EXISTS insumos (
          id INTEGER PRIMARY KEY AUTOINCREMENT, nome TEXT NOT NULL,
          unidade TEXT NOT NULL, quantidade_embalagem REAL NOT NULL DEFAULT 1.0,
          preco_total REAL NOT NULL DEFAULT 0.0, preco_unitario REAL NOT NULL
      )""",
      """CREATE TABLE IF NOT EXISTS marmitas (
          id INTEGER PRIMARY KEY AUTOINCREMENT, nome TEXT NOT NULL
      )""",
      """CREATE TABLE IF NOT EXISTS ficha_tecnica (
          id INTEGER PRIMARY KEY AUTOINCREMENT, marmita_id INTEGER NOT NULL,
          insumo_id INTEGER NOT NULL, quantidade REAL NOT NULL
      )""",
      """CREATE TABLE IF NOT EXISTS vendas (
          id INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT NOT NULL,
          marmita_id INTEGER NOT NULL, quantidade INTEGER NOT NULL,
          valor_unitario REAL NOT NULL, valor_total REAL NOT NULL, cliente TEXT
      )""",
      """CREATE TABLE IF NOT EXISTS kombito_migracoes (
          versao INTEGER PRIMARY KEY
      )""",
      """CREATE TABLE IF NOT EXISTS rendimentos_insumos (
          insumo_id INTEGER PRIMARY KEY,
          peso_cru_g REAL NOT NULL CHECK (peso_cru_g > 0),
          peso_pronto_g REAL NOT NULL CHECK (peso_pronto_g > 0)
      )""",
      """CREATE TRIGGER IF NOT EXISTS kombito_excluir_rendimento
          AFTER DELETE ON insumos
          BEGIN
            DELETE FROM rendimentos_insumos WHERE insumo_id = OLD.id;
          END""",
  ])
  if client.execute("SELECT 1 FROM kombito_migracoes WHERE versao = 1").rows:
    return

  alteracoes = []
  for tabela, campos in {
      "insumos": {
          "quantidade_embalagem": "REAL NOT NULL DEFAULT 1.0",
          "preco_total": "REAL NOT NULL DEFAULT 0.0",
      },
      "vendas": {
          "marmita_nome": "TEXT",
          "custo_unitario": "REAL",
          "custo_estimado": "INTEGER NOT NULL DEFAULT 0",
      },
  }.items():
    existentes = {r[1] for r in client.execute(f"PRAGMA table_info({tabela})").rows}
    for coluna, tipo in campos.items():
      if coluna not in existentes:
        alteracoes.append(f"ALTER TABLE {tabela} ADD COLUMN {coluna} {tipo}")
        if coluna == "preco_total":
          alteracoes.append("UPDATE insumos SET preco_total = preco_unitario * quantidade_embalagem")

  client.batch([
      *alteracoes,
      "CREATE INDEX IF NOT EXISTS ficha_insumo_idx ON ficha_tecnica(insumo_id)",
      "CREATE INDEX IF NOT EXISTS ficha_marmita_idx ON ficha_tecnica(marmita_id)",
      f"""WITH custos AS ({CONSULTA_CUSTOS})
          UPDATE vendas SET
            marmita_nome = COALESCE(
              (SELECT nome FROM marmitas WHERE id = vendas.marmita_id),
              'Marmita excluída #' || marmita_id
            ),
            custo_unitario = (SELECT custo FROM custos WHERE marmita_id = vendas.marmita_id),
            custo_estimado = 1
          WHERE marmita_nome IS NULL""",
      """CREATE TRIGGER IF NOT EXISTS kombito_proteger_insumo
          BEFORE DELETE ON insumos
          WHEN EXISTS (SELECT 1 FROM ficha_tecnica WHERE insumo_id = OLD.id)
          BEGIN
            SELECT RAISE(ABORT, 'INSUMO_EM_USO');
          END""",
      """CREATE TRIGGER IF NOT EXISTS kombito_validar_ficha
          BEFORE INSERT ON ficha_tecnica
          WHEN NOT EXISTS (SELECT 1 FROM insumos WHERE id = NEW.insumo_id)
            OR NOT EXISTS (SELECT 1 FROM marmitas WHERE id = NEW.marmita_id)
          BEGIN
            SELECT RAISE(ABORT, 'INSUMO_OU_MARMITA_INDISPONIVEL');
          END""",
      """CREATE TRIGGER IF NOT EXISTS kombito_excluir_ficha
          AFTER DELETE ON marmitas
          BEGIN
            DELETE FROM ficha_tecnica WHERE marmita_id = OLD.id;
          END""",
      "INSERT OR IGNORE INTO kombito_migracoes (versao) VALUES (1)",
  ])


def migrar_rendimentos(client):
  # Os nomes legados do cadastro são mantidos para compatibilidade.
  # Os valores usam a unidade de preparo de cada insumo: g, ml ou un.
  if client.execute("SELECT 1 FROM kombito_migracoes WHERE versao = 2").rows:
    return

  client.batch([
      "ALTER TABLE ficha_tecnica ADD COLUMN quantidade_pronta REAL",
      "ALTER TABLE ficha_tecnica ADD COLUMN fator_compra REAL",
      *[
          f"""CREATE TRIGGER kombito_recalcular_rendimento_{evento.lower()}
              AFTER {evento} ON rendimentos_insumos
              BEGIN
                UPDATE ficha_tecnica
                SET quantidade = quantidade_pronta * (NEW.peso_cru_g / NEW.peso_pronto_g) / fator_compra
                WHERE insumo_id = NEW.insumo_id AND quantidade_pronta IS NOT NULL;
              END"""
          for evento in ("INSERT", "UPDATE")
      ],
      "INSERT INTO kombito_migracoes (versao) VALUES (2)",
  ])


@st.cache_resource
def inicializar_banco(url, _token):
  with libsql_client.create_client_sync(url=url, auth_token=_token) as client:
    for versao, migracao in ((1, migrar_banco), (2, migrar_rendimentos)):
      try:
        migracao(client)
      except libsql_client.LibsqlError:
        # Outra sessão pode ter concluído a migração enquanto esta lia o esquema.
        if not client.execute("SELECT 1 FROM kombito_migracoes WHERE versao = ?", [versao]).rows:
          raise
        logging.info("Migração %s do Kombito concluída por outra sessão.", versao)
  return True


def consultar_ficha(marmita_id):
  return executar(
      """SELECT f.id, COALESCE(i.nome, 'Insumo removido #' || f.insumo_id),
                f.quantidade, i.unidade, f.quantidade_pronta, f.fator_compra,
                f.quantidade * i.preco_unitario, f.insumo_id, r.insumo_id IS NOT NULL
         FROM ficha_tecnica AS f
         LEFT JOIN insumos AS i ON i.id = f.insumo_id
         LEFT JOIN rendimentos_insumos AS r ON r.insumo_id = f.insumo_id
         WHERE f.marmita_id = ? ORDER BY f.id""",
      [marmita_id],
  ).rows


def tabela_ficha(ficha):
  tabela = []
  for id_item, nome, quantidade, unidade, pronta, fator_compra, custo, _, ativo in ficha:
    grupo = UNIDADES.get((unidade or "").strip().lower(), (None, 1))[0]
    antes = quantidade * fator_compra if pronta is not None else None
    tabela.append({
        "ID": id_item,
        "Insumo": nome,
        "Qtd. na unidade de compra": quantidade,
        "Unidade de compra": unidade,
        "Antes do preparo": antes,
        "Após o preparo": pronta,
        "Unidade de preparo": UNIDADES_PREPARO.get(grupo) if antes is not None else None,
        "Rendimento (%)": 100 * pronta / antes if antes else None,
        "Variação (%)": 100 * (pronta / antes - 1) if antes else None,
        "Diferença (pronto − cru)": pronta - antes if antes is not None else None,
        "Vínculo": ("Ativo" if ativo else "Último rendimento aplicado") if antes is not None else None,
        "Custo (R$)": custo,
    })
  return pd.DataFrame(tabela).round({
      "Qtd. na unidade de compra": 6, "Antes do preparo": 3, "Após o preparo": 3,
      "Rendimento (%)": 2, "Variação (%)": 2, "Diferença (pronto − cru)": 3, "Custo (R$)": 2,
  })


def apagar(sql, registro_id, sucesso):
  resultado = executar(sql, [registro_id])
  if resultado.rows_affected != 1:
    st.warning("O registro já foi removido. Atualize a página.")
    return
  st.session_state["mensagem_sucesso"] = sucesso
  st.rerun()


try:
  inicializar_banco(st.secrets["TURSO_DATABASE_URL"], st.secrets["TURSO_AUTH_TOKEN"])
except Exception:
  logging.exception("Falha ao preparar ou migrar o banco do Kombito")
  st.error("Não foi possível preparar o banco. Verifique a conexão com o Turso e consulte o log do aplicativo antes de tentar novamente.")
  st.stop()

if mensagem := st.session_state.pop("mensagem_sucesso", None):
  st.success(mensagem)

# Criando as Abas do Sistema
aba1, aba2, aba3, aba4 = st.tabs([
    "Insumos",
    "Receitas",
    "Precificação",
    "Vendas",
])

# ==========================================
# ABA 1: INSUMOS E EMBALAGENS
# ==========================================
with aba1:
  st.header("Insumos cadastrados")
  lista_insumos = st.container()

  with st.expander("Cadastrar insumo"):
    with st.form("form_insumo", clear_on_submit=True):
      col1, col2, col3, col4 = st.columns([2, 1, 1, 1])
      with col1:
        nome_insumo = st.text_input("Nome do Item (ex: Arroz Agulinha 5kg)")
      with col2:
        unidade = st.selectbox("Unidade de Compra", list(UNIDADES))
      with col3:
        qtde_embalagem = st.number_input(
            "Qtd/Peso da Embalagem (ex: 5 para 5kg)",
            min_value=0.01,
            value=1.0,
            format="%.2f",
        )
      with col4:
        preco_total = st.number_input(
            "Preço Total Pago (R$)", min_value=0.0, format="%.2f", step=1.0
        )

      enviado_insumo = st.form_submit_button("Salvar Insumo")

      if enviado_insumo:
        if nome_insumo.strip() == "":
          st.warning("Por favor, digite o nome do insumo.")
        elif not embalagem_valida(qtde_embalagem, preco_total):
          st.warning("Informe uma quantidade positiva e um preço válido.")
        else:
          try:
            # Preço unitário base (por kg, por grama, por litro, etc)
            preco_unitario = preco_total / qtde_embalagem
            executar(
                "INSERT INTO insumos (nome, unidade, quantidade_embalagem, preco_total, preco_unitario) VALUES (?, ?, ?, ?, ?)",
                [
                    nome_insumo.strip(),
                    unidade,
                    float(qtde_embalagem),
                    float(preco_total),
                    float(preco_unitario),
                ],
            )
            st.success(f"Insumo '{nome_insumo}' salvo com sucesso!")
            st.rerun()
          except Exception as e:
            st.error(f"Erro ao salvar insumo: {e}")

  try:
    rs = executar(
        "SELECT id, nome, unidade, quantidade_embalagem, preco_total, preco_unitario FROM insumos"
    )
    if rs.rows:
      df_insumos = pd.DataFrame(
          [
              {
                  "ID": r[0],
                  "Item": r[1],
                  "Unidade": r[2],
                  "Tamanho Emb.": r[3],
                  "Valor Pago (R$)": r[4],
                  "Custo Base (R$)": r[5],
              }
              for r in rs.rows
          ]
      )
      lista_insumos.dataframe(df_insumos, width="stretch", hide_index=True)
      nomes_insumos = {r[0]: f"{r[1]} (ID {r[0]})" for r in rs.rows}
      insumos_por_id = {r[0]: r for r in rs.rows}
      with st.expander("Atualizar preço"):
        insumo_preco_id = st.selectbox(
            "Insumo para atualizar o preço", list(nomes_insumos),
            format_func=nomes_insumos.__getitem__,
        )
        _, _, unidade_preco, tamanho_atual, preco_atual, custo_atual = insumos_por_id[insumo_preco_id]
        st.caption(f"Custo atual de R$ {custo_atual:.4f} por {unidade_preco}.")
        with st.form(f"atualizar_preco_{insumo_preco_id}_{tamanho_atual}_{preco_atual}"):
          col_tamanho, col_preco = st.columns(2)
          novo_tamanho = col_tamanho.number_input(
              f"Tamanho da embalagem ({unidade_preco})",
              min_value=0.0, value=float(tamanho_atual), step=0.1, format="%.3f",
          )
          novo_preco = col_preco.number_input(
              "Valor pago pela embalagem (R$)",
              min_value=0.0, value=float(preco_atual), step=0.01, format="%.2f",
          )
          if st.form_submit_button("Salvar novo preço"):
            if not embalagem_valida(novo_tamanho, novo_preco):
              st.warning("Informe um tamanho de embalagem maior que zero e um preço válido.")
            else:
              resultado = executar(
                  """UPDATE insumos
                     SET quantidade_embalagem = ?, preco_total = ?, preco_unitario = ?
                     WHERE id = ?""",
                  [novo_tamanho, novo_preco, novo_preco / novo_tamanho, insumo_preco_id],
              )
              if resultado.rows_affected == 1:
                st.session_state["mensagem_sucesso"] = "Preço do insumo atualizado."
                st.rerun()
              st.warning("Este insumo já foi excluído. Atualize a página.")

      unidades_preparo = {
          r[0]: UNIDADES_PREPARO[grupo]
          for r in rs.rows
          if (grupo := UNIDADES.get(r[2].strip().lower(), (None, 1))[0]) in UNIDADES_PREPARO
      }
      if unidades_preparo:
        with st.expander("Rendimento no preparo"):
          rendimento_id = st.selectbox(
              "Insumo para cadastrar o rendimento", list(unidades_preparo),
              format_func=nomes_insumos.__getitem__,
          )
          unidade_preparo = unidades_preparo[rendimento_id]
          rendimento_salvo = executar(
              "SELECT peso_cru_g, peso_pronto_g FROM rendimentos_insumos WHERE insumo_id = ?",
              [rendimento_id],
          ).rows
          antes_salvo, depois_salvo = rendimento_salvo[0] if rendimento_salvo else (None, None)
          if rendimento_salvo:
            percentual = 100 * depois_salvo / antes_salvo
            st.caption(
                f"Rendimento atual de {percentual:.2f}% · "
                f"Variação de {percentual - 100:+.2f}% "
                f"({depois_salvo - antes_salvo:+g} {unidade_preparo})"
            )
          with st.form(f"rendimento_{rendimento_id}_{antes_salvo}_{depois_salvo}"):
            col_antes, col_depois = st.columns(2)
            antes = col_antes.number_input(
                f"Quantidade antes do preparo ({unidade_preparo})",
                min_value=0.001, value=antes_salvo, step=1.0, format="%.3f",
            )
            depois = col_depois.number_input(
                f"Quantidade após o preparo ({unidade_preparo})",
                min_value=0.001, value=depois_salvo, step=1.0, format="%.3f",
            )
            confirmar_atipico = st.checkbox("Confirmei as medidas de um rendimento atípico")
            if st.form_submit_button("Salvar rendimento"):
              if not all(qtd is not None and math.isfinite(qtd) and qtd > 0 for qtd in (antes, depois)):
                st.warning("Informe duas quantidades positivas e válidas.")
              elif not math.isfinite(100 * (depois / antes)) or not math.isfinite(100 * (antes / depois)):
                st.warning("A proporção entre as quantidades é grande demais para o cálculo.")
              elif not 0.25 <= depois / antes <= 4 and not confirmar_atipico:
                st.warning(
                    f"O rendimento é de {100 * (depois / antes):.2f}%. Confira as medidas em "
                    f"{unidade_preparo} e confirme se esse resultado corresponde ao preparo."
                )
              else:
                resultado = executar(
                    """INSERT INTO rendimentos_insumos (insumo_id, peso_cru_g, peso_pronto_g)
                       SELECT id, ?, ? FROM insumos WHERE id = ?
                       ON CONFLICT(insumo_id) DO UPDATE SET
                         peso_cru_g = excluded.peso_cru_g,
                         peso_pronto_g = excluded.peso_pronto_g""",
                    [antes, depois, rendimento_id],
                )
                if resultado.rows_affected == 1:
                  st.session_state["mensagem_sucesso"] = "Rendimento salvo."
                  st.rerun()
                st.warning("Este insumo já foi excluído. Atualize a página.")
          if rendimento_salvo:
            confirmar_remocao = st.checkbox(
                "Confirmo a remoção do rendimento cadastrado", key=f"remover_rendimento_{rendimento_id}",
                help="As fichas mantêm a última conversão aplicada até que um novo rendimento seja cadastrado.",
            )
            if st.button("Remover rendimento", disabled=not confirmar_remocao):
              apagar(
                  "DELETE FROM rendimentos_insumos WHERE insumo_id = ?", rendimento_id,
                  "Rendimento removido.",
              )

      with st.expander("Excluir insumo"):
        insumo_para_excluir = st.selectbox(
            "Selecione o insumo para apagar", list(nomes_insumos),
            format_func=nomes_insumos.__getitem__, key="insumo_excluir",
        )
        em_uso = executar(
            "SELECT 1 FROM ficha_tecnica WHERE insumo_id = ? LIMIT 1", [insumo_para_excluir]
        ).rows
        if em_uso:
          st.warning("Este insumo está em uma ficha técnica. Remova-o da receita antes de excluí-lo.")
        elif st.button("Excluir Insumo Selecionado", type="primary"):
          try:
            apagar("DELETE FROM insumos WHERE id = ?", insumo_para_excluir, "Insumo excluído.")
          except libsql_client.LibsqlError as exc:
            if "INSUMO_EM_USO" in str(exc):
              st.warning("Este insumo passou a ser usado em uma ficha técnica.")
            else:
              raise

    else:
      lista_insumos.info("Nenhum insumo cadastrado. Abra **Cadastrar insumo** para começar.")
  except Exception as e:
    st.error(f"Erro ao carregar insumos: {e}")

# ==========================================
# ABA 2: MARMITAS E FICHA TÉCNICA
# ==========================================
with aba2:
  st.header("Receitas por marmita")

  with st.expander("Criar marmita"):
    with st.form("form_marmita", clear_on_submit=True):
      nome_marmita = st.text_input("Nome do Prato/Marmita (ex: Frango Grelhado com Legumes)")
      if st.form_submit_button("Criar Nova Marmita"):
        if not nome_marmita.strip():
          st.warning("Digite o nome da marmita.")
        else:
          try:
            executar("INSERT INTO marmitas (nome) VALUES (?)", [nome_marmita.strip()])
            st.session_state["mensagem_sucesso"] = "Marmita criada."
            st.rerun()
          except Exception as exc:
            st.error(f"Erro ao criar marmita: {exc}")

  try:
    marmitas = {
        r[0]: f"{r[1]} (ID {r[0]})"
        for r in executar("SELECT id, nome FROM marmitas ORDER BY nome, id").rows
    }
    insumos = {
        r[0]: r for r in executar(
            "SELECT id, nome, unidade, quantidade_embalagem, preco_total FROM insumos ORDER BY nome, id"
        ).rows
    }
    rotulos_insumos = {
        id_insumo: f"{r[1]} ({r[3]:g} {r[2]} por R$ {r[4]:.2f}, ID {id_insumo})"
        for id_insumo, r in insumos.items()
    }

    if not marmitas:
      st.info("Cadastre uma marmita para montar a receita.")
    else:
      marmita_id = st.selectbox(
          "Selecione a Marmita", list(marmitas), format_func=marmitas.__getitem__, key="receita_marmita",
      )
      painel_ficha = st.container()
      if not insumos:
        st.info("Cadastre insumos na aba Insumos para montar a receita.")
      else:
        with st.expander("Adicionar ingrediente"):
          insumo_id = st.selectbox(
              "Selecione o Insumo/Embalagem", list(insumos),
              format_func=rotulos_insumos.__getitem__, key="receita_insumo",
          )
          unidade_compra = insumos[insumo_id][2].strip().lower()
          grupo, fator_compra = UNIDADES.get(unidade_compra, (unidade_compra, 1))
          rendimento = executar(
              "SELECT peso_cru_g, peso_pronto_g FROM rendimentos_insumos WHERE insumo_id = ?",
              [insumo_id],
          ).rows
          usar_rendimento = bool(rendimento) and st.checkbox(
              "A quantidade usada é do insumo já preparado",
              key=f"usar_rendimento_{insumo_id}",
                help="Marque se você mediu a porção depois do preparo. O Kombito usa o rendimento para calcular o custo.",
          )
          if usar_rendimento:
            antes, depois = rendimento[0]
            medida = UNIDADES_PREPARO[grupo]
            st.caption(f"Rendimento cadastrado de {antes:g} {medida} antes para {depois:g} {medida} após o preparo.")
          opcoes = [
              unidade for unidade, (tipo, _) in UNIDADES.items()
              if not usar_rendimento or tipo == grupo
          ]
          if unidade_compra not in UNIDADES:
            opcoes.append(unidade_compra)
          padrao = {"massa": "g", "volume": "ml", "contagem": "un"}.get(grupo, unidade_compra)

          col_qtd, col_unidade = st.columns(2)
          with col_qtd:
            quantidade = st.number_input(
                "Quantidade pronta usada na marmita" if usar_rendimento else "Quantidade usada na marmita",
                min_value=0.001, value=1.0,
                step=0.001, format="%.3f",
            )
          with col_unidade:
            unidade_uso = st.selectbox(
                "Unidade de medida que você usou", opcoes, index=opcoes.index(padrao),
                format_func=lambda unidade: {"un": "unidade (un)", "litro": "litro (L)"}.get(unidade, unidade),
                key=f"receita_unidade_{insumo_id}" if not usar_rendimento else f"receita_unidade_pronta_{insumo_id}",
            )
          grupo_uso, fator_uso = UNIDADES.get(unidade_uso, (unidade_uso, 1))
          if grupo_uso == grupo:
            fator_conversao = fator_uso / fator_compra
          else:
            fator_conversao = st.number_input(
                f"Equivalência em {unidade_compra} para 1 {unidade_uso}",
                min_value=0.0, value=None, step=0.001, format="%.6f",
                help="Informe quanto uma unidade da receita pesa, mede ou rende na unidade de compra deste insumo.",
                key=f"receita_equivalencia_{insumo_id}_{unidade_uso}",
            )
          if st.button("Adicionar à Receita", type="primary"):
            quantidade_base = quantidade * fator_conversao if fator_conversao is not None else None
            quantidade_pronta = quantidade * fator_uso
            if quantidade_base is None or not math.isfinite(quantidade_base) or quantidade_base <= 0:
              st.warning("Informe uma quantidade positiva e, quando necessário, uma equivalência válida.")
            elif usar_rendimento:
              if not math.isfinite(quantidade_pronta):
                st.warning("Informe uma quantidade pronta válida.")
              else:
                resultado = executar(
                    """INSERT INTO ficha_tecnica
                          (marmita_id, insumo_id, quantidade, quantidade_pronta, fator_compra)
                       SELECT ?, insumo_id, ? * (peso_cru_g / peso_pronto_g), ?, ?
                       FROM rendimentos_insumos WHERE insumo_id = ?""",
                    [marmita_id, quantidade_base, quantidade_pronta, fator_compra, insumo_id],
                )
                if resultado.rows_affected == 1:
                  st.session_state["mensagem_sucesso"] = "Ingrediente adicionado à receita."
                  st.rerun()
                st.warning("O rendimento foi removido. Atualize a página.")
            else:
              executar(
                  "INSERT INTO ficha_tecnica (marmita_id, insumo_id, quantidade) VALUES (?, ?, ?)",
                  [marmita_id, insumo_id, quantidade_base],
              )
              st.session_state["mensagem_sucesso"] = "Ingrediente adicionado à receita."
              st.rerun()

      ficha = consultar_ficha(marmita_id)
      painel_ficha.subheader("Ficha técnica")
      if ficha:
        tabela = tabela_ficha(ficha)
        painel_ficha.dataframe(
            tabela[["Insumo", "Qtd. na unidade de compra", "Unidade de compra",
                    "Após o preparo", "Unidade de preparo", "Custo (R$)"]].rename(
                columns={"Qtd. na unidade de compra": "Qtd. para custeio"}
            ),
            hide_index=True, width="stretch",
        )
        with painel_ficha.expander("Ver detalhes do rendimento"):
          st.dataframe(tabela, hide_index=True, width="stretch")
        itens = {r[0]: f"{r[1]} (item {r[0]})" for r in ficha}
        vinculaveis = {r[0]: r[3] for r in ficha if r[4] is None and r[8]}
        if vinculaveis:
          with st.expander("Vincular rendimento a um ingrediente existente"):
            item_vinculo = st.selectbox(
                "Ingrediente da ficha técnica", list(vinculaveis), format_func=itens.__getitem__,
            )
            grupo_item, fator_item = UNIDADES[vinculaveis[item_vinculo].strip().lower()]
            with st.form(f"vincular_rendimento_{item_vinculo}"):
              pronta = st.number_input(
                  f"Quantidade pronta usada na marmita ({UNIDADES_PREPARO[grupo_item]})",
                  min_value=0.001, value=None, step=1.0, format="%.3f",
              )
              if st.form_submit_button("Vincular e recalcular custo"):
                if pronta is None or not math.isfinite(pronta) or pronta <= 0:
                  st.warning("Informe a quantidade pronta efetivamente usada na marmita.")
                else:
                  resultado = executar(
                      """UPDATE ficha_tecnica AS f
                         SET quantidade = ? * (r.peso_cru_g / r.peso_pronto_g) / ?,
                             quantidade_pronta = ?, fator_compra = ?
                         FROM rendimentos_insumos AS r
                         WHERE f.id = ? AND f.quantidade_pronta IS NULL AND f.insumo_id = r.insumo_id""",
                      [pronta, fator_item, pronta, fator_item, item_vinculo],
                  )
                  if resultado.rows_affected == 1:
                    st.session_state["mensagem_sucesso"] = "Rendimento vinculado e custo recalculado."
                    st.rerun()
                  st.warning("O ingrediente ou o rendimento mudou. Atualize a página.")
        with st.expander("Remover ingrediente"):
          item_id = st.selectbox("Ingrediente a remover", list(itens), format_func=itens.__getitem__)
          confirmar_item = st.checkbox("Confirmo a remoção deste ingrediente", key=f"confirma_item_{item_id}")
          if st.button("Remover ingrediente", disabled=not confirmar_item):
            apagar("DELETE FROM ficha_tecnica WHERE id = ?", item_id, "Ingrediente removido da receita.")

      elif insumos:
        painel_ficha.info("A ficha técnica está vazia. Abra **Adicionar ingrediente** para montar a receita.")

      with st.expander("Excluir marmita"):
        st.write("A exclusão remove a ficha técnica da marmita e mantém as vendas registradas.")
        confirmar_marmita = st.checkbox(
            f"Confirmo a exclusão de {marmitas[marmita_id]}", key=f"confirma_marmita_{marmita_id}",
        )
        if st.button("Excluir Marmita", type="primary", disabled=not confirmar_marmita):
          apagar("DELETE FROM marmitas WHERE id = ?", marmita_id, "Marmita e ficha técnica excluídas.")

  except Exception as exc:
    st.error(f"Erro ao gerenciar receitas: {exc}")

# ==========================================
# ABA 3: CUSTOS E PRECIFICAÇÃO
# ==========================================
with aba3:
  st.header("Custos e preços por marmita")
  acrescimo = st.slider("Acréscimo sobre o custo (%)", 10, 300, 100, step=10)
  try:
    custos = executar(f"""
        SELECT m.id, m.nome, c.custo FROM marmitas AS m
        JOIN ({CONSULTA_CUSTOS}) AS c ON c.marmita_id = m.id
        ORDER BY m.nome, m.id
    """).rows
    if custos:
      tabela = pd.DataFrame(
          [tuple(r) for r in custos], columns=["ID", "Marmita", "Custo de Produção (R$)"],
      )
      custo = tabela["Custo de Produção (R$)"]
      tabela["Preço de Venda Sugerido (R$)"] = custo * (1 + acrescimo / 100)
      tabela["Lucro Estimado (R$)"] = custo * acrescimo / 100
      st.dataframe(tabela.round(2), width="stretch", hide_index=True)
      with st.expander("Detalhamento do custo e do rendimento por ingrediente"):
        marmitas_custo = {r[0]: f"{r[1]} (ID {r[0]})" for r in custos}
        marmita_custo_id = st.selectbox(
            "Marmita para detalhar", list(marmitas_custo), format_func=marmitas_custo.__getitem__,
        )
        ficha_custo = consultar_ficha(marmita_custo_id)
        if ficha_custo:
          st.dataframe(tabela_ficha(ficha_custo), width="stretch", hide_index=True)
    else:
      st.info("Cadastre marmitas e insumos nas fichas técnicas para ver os custos.")
  except Exception as exc:
    st.error(f"Erro ao calcular custos: {exc}")

# ==========================================
# ABA 4: VENDAS & FINANCEIRO
# ==========================================
with aba4:
  st.header("Vendas e resultados")

  try:
    marmitas_venda = {
        r[0]: f"{r[1]} (ID {r[0]})"
        for r in executar("SELECT id, nome FROM marmitas ORDER BY nome, id").rows
    }
    if not marmitas_venda:
      st.info("Cadastre uma marmita para registrar novas vendas.")
    else:
      with st.expander("Registrar venda"):
        with st.form("form_venda", clear_on_submit=True):
          col_v1, col_v2, col_v3 = st.columns(3)
          with col_v1:
            data_venda = st.date_input("Data da Venda", datetime.date.today())
            marmita_vendida = st.selectbox(
                "Marmita", list(marmitas_venda), format_func=marmitas_venda.__getitem__,
                key="venda_marmita",
            )
          with col_v2:
            qtd_vendida = st.number_input("Quantidade", min_value=1, value=1, step=1)
            valor_un = st.number_input(
                "Valor Unitário cobrado (R$)", min_value=0.0, value=20.0, format="%.2f",
            )
          with col_v3:
            nome_cliente = st.text_input("Nome do Cliente (Opcional)", placeholder="Ex: João")

          if st.form_submit_button("Registrar Venda"):
            total = qtd_vendida * valor_un
            if not (isinstance(data_venda, datetime.date) and qtd_vendida >= 1
                    and math.isfinite(valor_un) and valor_un >= 0 and math.isfinite(total)):
              st.warning("Informe uma data, uma quantidade positiva e um preço válido.")
            else:
              resultado = executar(
                  f"""INSERT INTO vendas (
                          data, marmita_id, quantidade, valor_unitario, valor_total,
                          cliente, marmita_nome, custo_unitario
                      )
                      SELECT ?, m.id, ?, ?, ?, ?, m.nome, c.custo FROM marmitas AS m
                      LEFT JOIN ({CONSULTA_CUSTOS}) AS c ON c.marmita_id = m.id
                      WHERE m.id = ?""",
                  [data_venda.isoformat(), qtd_vendida, valor_un, total, nome_cliente.strip(), marmita_vendida],
              )
              if resultado.rows_affected == 1:
                st.session_state["mensagem_sucesso"] = "Venda registrada."
                st.rerun()
              st.warning("Esta marmita já foi excluída. Atualize a página.")

    st.divider()
    vendas = executar("""
        SELECT id, data, marmita_id, quantidade, valor_unitario, valor_total, cliente,
               COALESCE(marmita_nome, 'Marmita #' || marmita_id), custo_unitario, custo_estimado
        FROM vendas ORDER BY data DESC, id DESC
    """)
    if vendas.rows:
      df_vendas = pd.DataFrame([tuple(r) for r in vendas.rows], columns=[
          "id", "data", "marmita_id", "quantidade", "valor_unitario",
          "valor_total", "cliente", "nome", "custo_unitario", "custo_estimado",
      ])
      df_vendas["lucro_total"] = (
          df_vendas["valor_total"] - pd.to_numeric(df_vendas["custo_unitario"]) * df_vendas["quantidade"]
      )
      df_vendas["marmita"] = (
          df_vendas["nome"] + " (ID " + df_vendas["marmita_id"].astype(str) + ")"
      )
      df_vendas["origem_custo"] = "Registrado na venda"
      df_vendas.loc[df_vendas["custo_estimado"].eq(1), "origem_custo"] = "Estimado na migração"
      df_vendas.loc[df_vendas["custo_unitario"].isna(), "origem_custo"] = "Indisponível"

      st.subheader("Resumo de vendas")
      faturamento_total = df_vendas["valor_total"].sum()
      lucro_liquido_total = df_vendas["lucro_total"].sum(min_count=1)
      total_marmitas_vendidas = df_vendas["quantidade"].sum()
      total_clientes = (
          df_vendas["cliente"].replace("", pd.NA).dropna().nunique()
      )

      m1, m2, m3, m4 = st.columns(4)
      m1.metric("Faturamento Total", f"R$ {faturamento_total:.2f}")
      m2.metric(
          "Lucro nas Vendas com Custo",
          f"R$ {lucro_liquido_total:.2f}" if pd.notna(lucro_liquido_total) else "Indisponível",
      )
      if df_vendas["origem_custo"].ne("Registrado na venda").any():
        st.caption("O lucro inclui custos estimados na migração e exclui vendas sem custo disponível.")
      m3.metric("Marmitas Vendidas", int(total_marmitas_vendidas))
      m4.metric("Clientes Únicos", int(total_clientes))

      st.divider()

      col_g1, col_g2 = st.columns(2)

      with col_g1:
        st.subheader("📅 Vendas por Dia (Faturamento)")
        vendas_por_dia = (
            df_vendas.groupby("data")["valor_total"].sum().reset_index()
        )
        fig_dia = px.bar(
            vendas_por_dia,
            x="data",
            y="valor_total",
            labels={
                "data": "Data",
                "valor_total": "Faturamento (R$)",
            },
            color_discrete_sequence=["#FF4B4B"],
        )
        st.plotly_chart(fig_dia, width="stretch")

      with col_g2:
        st.subheader("🍲 Marmitas Mais Vendidas (Quantidade)")
        vendas_por_prato = (
            df_vendas.groupby("marmita")["quantidade"].sum().reset_index()
        )
        fig_prato = px.pie(
            vendas_por_prato,
            names="marmita",
            values="quantidade",
            hole=0.4,
        )
        st.plotly_chart(fig_prato, width="stretch")

      st.divider()
      st.subheader("Histórico de vendas")

      rotulos_vendas = {
          r.id: f"Venda {r.id} | {r.data} | {r.marmita} ({r.quantidade} un)"
          for r in df_vendas.itertuples()
      }
      tabela = df_vendas[[
          "id", "data", "marmita", "quantidade", "valor_unitario",
          "valor_total", "lucro_total", "origem_custo", "cliente",
      ]].copy()
      tabela.columns = [
          "ID", "Data", "Marmita", "Qtd", "Preço Un. (R$)",
          "Total (R$)", "Lucro (R$)", "Origem do custo", "Cliente",
      ]
      st.dataframe(tabela.round(2), width="stretch", hide_index=True)
      with st.expander("Excluir venda"):
        venda_id = st.selectbox(
            "Selecione a venda", list(rotulos_vendas), format_func=rotulos_vendas.__getitem__,
        )
        confirmar_venda = st.checkbox("Confirmo a exclusão desta venda", key=f"confirma_venda_{venda_id}")
        if st.button("Excluir Venda Selecionada", type="primary", disabled=not confirmar_venda):
          apagar("DELETE FROM vendas WHERE id = ?", venda_id, "Venda excluída.")
    else:
      st.info("Nenhuma venda registrada ainda.")
  except Exception as exc:
    st.error(f"Erro ao carregar o financeiro: {exc}")
