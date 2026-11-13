import datetime
import libsql_client
import pandas as pd
import plotly.express as px
import streamlit as st

st.set_page_config(
    page_title="Kombito - Gestão e Finanças", page_icon="🍲", layout="wide"
)

st.title("🍲 Kombito - Gestão Completa de Custos e Vendas")

# Função para conectar ao Turso usando os segredos
def get_turso_client():
  url = st.secrets["TURSO_DATABASE_URL"]
  token = st.secrets["TURSO_AUTH_TOKEN"]
  return libsql_client.create_client_sync(url=url, auth_token=token)

# Inicializar todas as tabelas no banco de dados se não existirem
try:
  client = get_turso_client()
  client.execute("""
        CREATE TABLE IF NOT EXISTS insumos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL,
            unidade TEXT NOT NULL,
            quantidade_embalagem REAL NOT NULL DEFAULT 1.0,
            preco_total REAL NOT NULL DEFAULT 0.0,
            preco_unitario REAL NOT NULL
        )
    """)
  # Atualiza banco antigo caso não tenha as colunas de embalagem
  try:
    client.execute("ALTER TABLE insumos ADD COLUMN quantidade_embalagem REAL DEFAULT 1.0")
    client.execute("ALTER TABLE insumos ADD COLUMN preco_total REAL DEFAULT 0.0")
  except Exception:
    pass

  client.execute("""
        CREATE TABLE IF NOT EXISTS marmitas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL
        )
    """)
  client.execute("""
        CREATE TABLE IF NOT EXISTS ficha_tecnica (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            marmita_id INTEGER NOT NULL,
            insumo_id INTEGER NOT NULL,
            quantidade REAL NOT NULL
        )
    """)
  client.execute("""
        CREATE TABLE IF NOT EXISTS vendas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data TEXT NOT NULL,
            marmita_id INTEGER NOT NULL,
            quantidade INTEGER NOT NULL,
            valor_unitario REAL NOT NULL,
            valor_total REAL NOT NULL,
            cliente TEXT
        )
    """)
  client.close()
except Exception as e:
  st.error(f"Erro ao inicializar o banco de dados: {e}")

# Criando as Abas do Sistema
aba1, aba2, aba3, aba4 = st.tabs([
    "🛒 Insumos",
    "🍲 Marmitas & Receitas",
    "💰 Precificação",
    "📈 Vendas & Financeiro",
])

# ==========================================
# ABA 1: INSUMOS E EMBALAGENS
# ==========================================
with aba1:
  st.header("Cadastro de Insumos e Embalagens")
  st.write("Cadastre o pacote fechado (ex: Saco de Arroz 5kg, Azeite 500ml).")

  with st.form("form_insumo", clear_on_submit=True):
    col1, col2, col3, col4 = st.columns([2, 1, 1, 1])
    with col1:
      nome_insumo = st.text_input("Nome do Item (ex: Arroz Agulinha 5kg)")
    with col2:
      unidade = st.selectbox("Unidade de Compra", ["kg", "g", "litro", "ml", "un"])
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
      else:
        try:
          # Preço unitário base (por kg, por grama, por litro, etc)
          preco_unitario = (
              preco_total / qtde_embalagem if qtde_embalagem > 0 else 0.0
          )
          client = get_turso_client()
          client.execute(
              "INSERT INTO insumos (nome, unidade, quantidade_embalagem, preco_total, preco_unitario) VALUES (?, ?, ?, ?, ?)",
              [
                  nome_insumo.strip(),
                  unidade,
                  float(qtde_embalagem),
                  float(preco_total),
                  float(preco_unitario),
              ],
          )
          client.close()
          st.success(f"Insumo '{nome_insumo}' salvo com sucesso!")
          st.rerun()
        except Exception as e:
          st.error(f"Erro ao salvar insumo: {e}")

  st.divider()
  st.subheader("📋 Insumos Cadastrados")
  try:
    client = get_turso_client()
    rs = client.execute(
        "SELECT id, nome, unidade, quantidade_embalagem, preco_total, preco_unitario FROM insumos"
    )
    client.close()
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
      st.dataframe(df_insumos, use_container_width=True, hide_index=True)

      st.write("---")
      st.subheader("🗑️ Excluir Insumo")
      insumos_dict = {f"{row[1]} (ID: {row[0]})": row[0] for row in rs.rows}
      insumo_para_excluir = st.selectbox(
          "Selecione o insumo para apagar", list(insumos_dict.keys())
      )
      if st.button("Excluir Insumo Selecionado", type="primary"):
        id_del = insumos_dict[insumo_para_excluir]
        try:
          client = get_turso_client()
          client.execute("DELETE FROM insumos WHERE id = ?", [id_del])
          client.close()
          st.success("Insumo excluído com sucesso!")
          st.rerun()
        except Exception as e:
          st.error(f"Erro ao excluir insumo: {e}")
    else:
      st.info("Nenhum insumo cadastrado ainda.")
  except Exception as e:
    st.error(f"Erro ao carregar insumos: {e}")

# ==========================================
# ABA 2: MARMITAS E FICHA TÉCNICA
# ==========================================
with aba2:
  st.header("Montagem de Marmitas e Receitas")

  with st.form("form_marmita", clear_on_submit=True):
    nome_marmita = st.text_input(
        "Nome do Prato/Marmita (ex: Frango Grelhado com Legumes)"
    )
    salvar_marmita = st.form_submit_button("Criar Nova Marmita")

    if salvar_marmita:
      if nome_marmita.strip() == "":
        st.warning("Digite o nome da marmita.")
      else:
        try:
          client = get_turso_client()
          client.execute(
              "INSERT INTO marmitas (nome) VALUES (?)", [nome_marmita.strip()]
          )
          client.close()
          st.success(f"Marmita '{nome_marmita}' criada com sucesso!")
          st.rerun()
        except Exception as e:
          st.error(f"Erro ao criar marmita: {e}")

  st.divider()
  st.subheader("Adicionar Ingrediente à Ficha Técnica")

  try:
    client = get_turso_client()
    res_marmitas = client.execute("SELECT id, nome FROM marmitas")
    res_insumos = client.execute(
        "SELECT id, nome, unidade, quantidade_embalagem, preco_total, preco_unitario FROM insumos"
    )
    client.close()

    marmitas_dict = {row[1]: row[0] for row in res_marmitas.rows}
    insumos_info = {
        row[0]: {
            "nome": row[1],
            "unidade": row[2],
            "qtde_emb": row[3],
        }
        for row in res_insumos.rows
    }
    insumos_dict = {
        f"{row[1]} (Emb. de {row[3]} {row[2]} por R$ {row[4]:.2f})": row[0]
        for row in res_insumos.rows
    }

    if not marmitas_dict:
      st.info("Cadastre pelo menos uma marmita acima para montar a receita.")
    elif not insumos_dict:
      st.info("Cadastre insumos na aba 'Insumos' antes de montar a receita.")
    else:
      # REMOVEMOS O FORMULÁRIO AQUI PARA AS UNIDADES ATUALIZAREM AO VIVO
      marmita_escolhida = st.selectbox(
          "Selecione a Marmita", list(marmitas_dict.keys())
      )
      insumo_escolhido_str = st.selectbox(
          "Selecione o Insumo/Embalagem", list(insumos_dict.keys())
      )

      i_id = insumos_dict[insumo_escolhido_str]
      unidade_compra = insumos_info[i_id]["unidade"].lower()

      col_qtd1, col_qtd2 = st.columns(2)
      with col_qtd1:
          quantidade_informada = st.number_input("Quantidade usada na marmita", min_value=0.01, value=1.0, step=1.0)
      with col_qtd2:
          # Mostra as opções corretas baseadas no que foi comprado
          if unidade_compra in ["kg", "g"]:
              opcoes_uso = ["g", "kg"]
          elif unidade_compra in ["litro", "ml"]:
              opcoes_uso = ["ml", "litro"]
          else:
              opcoes_uso = ["un"]
          
          unidade_uso = st.selectbox("Unidade de medida que você usou", opcoes_uso)

      salvar_ficha = st.button("Adicionar à Receita", type="primary")

      if salvar_ficha:
        m_id = marmitas_dict[marmita_escolhida]
        qtd_salvar = float(quantidade_informada)
        
        # Conversão invisível para bater com a unidade de compra no financeiro
        if unidade_compra == "kg" and unidade_uso == "g":
            qtd_salvar = quantidade_informada / 1000.0
        elif unidade_compra == "g" and unidade_uso == "kg":
            qtd_salvar = quantidade_informada * 1000.0
        elif unidade_compra == "litro" and unidade_uso == "ml":
            qtd_salvar = quantidade_informada / 1000.0
        elif unidade_compra == "ml" and unidade_uso == "litro":
            qtd_salvar = quantidade_informada * 1000.0

        try:
          client = get_turso_client()
          client.execute(
              "INSERT INTO ficha_tecnica (marmita_id, insumo_id, quantidade) VALUES (?, ?, ?)",
              [m_id, i_id, qtd_salvar],
          )
          client.close()
          st.success("Ingrediente adicionado à receita com sucesso!")
          st.rerun()
        except Exception as e:
          st.error(f"Erro ao salvar ficha técnica: {e}")

      st.divider()
      st.subheader("🗑️ Gerenciar Marmitas e Receitas")
      marmita_del = st.selectbox(
          "Selecione a marmita para excluir (remove ela e sua ficha técnica)",
          list(marmitas_dict.keys()),
      )
      if st.button("Excluir Marmita", type="primary"):
        m_id_del = marmitas_dict[marmita_del]
        try:
          client = get_turso_client()
          client.execute(
              "DELETE FROM ficha_tecnica WHERE marmita_id = ?", [m_id_del]
          )
          client.execute("DELETE FROM marmitas WHERE id = ?", [m_id_del])
          client.close()
          st.success("Marmita excluída com sucesso!")
          st.rerun()
        except Exception as e:
          st.error(f"Erro ao excluir marmita: {e}")

  except Exception as e:
    st.error(f"Erro ao carregar dados para receitas: {e}")

# ==========================================
# ABA 3: CUSTOS E PRECIFICAÇÃO
# ==========================================
with aba3:
  st.header("Análise de Custos e Sugestão de Preço de Venda")
  margem_lucro = st.slider(
      "Margem de Lucro Desejada (%)",
      min_value=10,
      max_value=300,
      value=100,
      step=10,
  )

  try:
    client = get_turso_client()
    df_m = (
        pd.DataFrame(
            [dict(zip(["id", "nome"], r)) for r in client.execute("SELECT * FROM marmitas").rows]
        )
        if client.execute("SELECT * FROM marmitas").rows
        else pd.DataFrame()
    )
    df_i = (
        pd.DataFrame(
            [
                dict(zip(["id", "nome", "unidade", "qtde_emb", "preco_tot", "preco"], r))
                for r in client.execute("SELECT id, nome, unidade, quantidade_embalagem, preco_total, preco_unitario FROM insumos").rows
            ]
        )
        if client.execute("SELECT id, nome, unidade, quantidade_embalagem, preco_total, preco_unitario FROM insumos").rows
        else pd.DataFrame()
    )
    df_ft = (
        pd.DataFrame(
            [
                dict(zip(["id", "marmita_id", "insumo_id", "qtd"], r))
                for r in client.execute("SELECT * FROM ficha_tecnica").rows
            ]
        )
        if client.execute("SELECT * FROM ficha_tecnica").rows
        else pd.DataFrame()
    )
    client.close()

    if not df_m.empty and not df_ft.empty and not df_i.empty:
      df_ft = df_ft.merge(
          df_m, left_on="marmita_id", right_on="id", suffixes=('', '_m')
      )
      df_ft = df_ft.merge(
          df_i, left_on="insumo_id", right_on="id", suffixes=('', '_i')
      )

      df_ft["custo_item"] = df_ft["preco"] * df_ft["qtd"]

      custos_por_marmita = (
          df_ft.groupby("nome")["custo_item"].sum().reset_index()
      )
      custos_por_marmita.columns = ["Marmita", "Custo de Produção (R$)"]

      custos_por_marmita["Preço de Venda Sugerido (R$)"] = custos_por_marmita[
          "Custo de Produção (R$)"
      ] * (1 + margem_lucro / 100)
      custos_por_marmita["Lucro Estimado (R$)"] = (
          custos_por_marmita["Preço de Venda Sugerido (R$)"]
          - custos_por_marmita["Custo de Produção (R$)"]
      )
      custos_por_marmita = custos_por_marmita.round(2)

      st.subheader("📊 Custos e Sugestão de Preços")
      st.dataframe(custos_por_marmita, use_container_width=True, hide_index=True)
    else:
      st.info(
          "Cadastre marmitas e insumos nas fichas técnicas para ver os custos."
      )
  except Exception as e:
    st.error(f"Erro ao calcular custos: {e}")

# ==========================================
# ABA 4: VENDAS & FINANCEIRO
# ==========================================
with aba4:
  st.header("📈 Controle de Vendas e Financeiro")
  st.write(
      "Registre as vendas feitas, acompanhe o faturamento e o lucro real do Kombito."
  )

  try:
    client = get_turso_client()
    df_m_vendas = (
        pd.DataFrame(
            [dict(zip(["id", "nome"], r)) for r in client.execute("SELECT * FROM marmitas").rows]
        )
        if client.execute("SELECT * FROM marmitas").rows
        else pd.DataFrame()
    )
    client.close()

    if df_m_vendas.empty:
      st.warning(
          "Você precisa cadastrar ao menos uma marmita antes de registrar vendas."
      )
    else:
      with st.form("form_venda", clear_on_submit=True):
        st.subheader("Registrar Nova Venda")
        col_v1, col_v2, col_v3 = st.columns(3)

        with col_v1:
          data_venda = st.date_input("Data da Venda", datetime.date.today())
          marmita_vendida = st.selectbox(
              "Marmita", df_m_vendas["nome"].tolist()
          )
        with col_v2:
          qtd_vendida = st.number_input(
              "Quantidade", min_value=1, value=1, step=1
          )
          valor_un = st.number_input(
              "Valor Unitário cobrado (R$)",
              min_value=0.0,
              value=20.0,
              format="%.2f",
          )
        with col_v3:
          nome_cliente = st.text_input(
              "Nome do Cliente (Opcional)", placeholder="Ex: João"
          )

        salvar_venda = st.form_submit_button("Registrar Venda")

        if salvar_venda:
          m_id = df_m_vendas[df_m_vendas["nome"] == marmita_vendida][
              "id"
          ].values[0]
          v_total = qtd_vendida * valor_un
          try:
            client = get_turso_client()
            client.execute(
                "INSERT INTO vendas (data, marmita_id, quantidade, valor_unitario, valor_total, cliente) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    str(data_venda),
                    int(m_id),
                    int(qtd_vendida),
                    float(valor_un),
                    float(v_total),
                    nome_cliente.strip() if nome_cliente else "",
                ],
            )
            client.close()
            st.success("Venda registrada com sucesso!")
            st.rerun()
          except Exception as e:
            st.error(f"Erro ao registrar venda: {e}")

      st.divider()

      client = get_turso_client()
      res_v = client.execute("SELECT * FROM vendas")
      client.close()

      if res_v.rows:
        df_vendas = pd.DataFrame(
            [
                dict(
                    zip(
                        [
                            "id",
                            "data",
                            "marmita_id",
                            "quantidade",
                            "valor_unitario",
                            "valor_total",
                            "cliente",
                        ],
                        r,
                    )
                )
                for r in res_v.rows
            ]
        )

        client = get_turso_client()
        df_m = pd.DataFrame(
            [dict(zip(["id", "nome"], r)) for r in client.execute("SELECT * FROM marmitas").rows]
        )
        df_i = pd.DataFrame(
            [
                dict(zip(["id", "nome", "unidade", "qtde_emb", "preco_tot", "preco"], r))
                for r in client.execute("SELECT id, nome, unidade, quantidade_embalagem, preco_total, preco_unitario FROM insumos").rows
            ]
        )
        df_ft = pd.DataFrame(
            [
                dict(zip(["id", "marmita_id", "insumo_id", "qtd"], r))
                for r in client.execute("SELECT * FROM ficha_tecnica").rows
            ]
        )
        client.close()

        if not df_ft.empty and not df_i.empty:
          df_ft = df_ft.merge(df_m, left_on="marmita_id", right_on="id")
          df_ft = df_ft.merge(df_i, left_on="insumo_id", right_on="id")
          df_ft["custo_item"] = df_ft["preco"] * df_ft["qtd"]
          custos_dict = (
              df_ft.groupby("marmita_id")["custo_item"].sum().to_dict()
          )
        else:
          custos_dict = {}

        df_vendas["custo_unitario"] = df_vendas["marmita_id"].map(
            custos_dict
        ).fillna(0.0)
        df_vendas["custo_total"] = (
            df_vendas["custo_unitario"] * df_vendas["quantidade"]
        )
        df_vendas["lucro_total"] = (
            df_vendas["valor_total"] - df_vendas["custo_total"]
        )
        df_vendas = df_vendas.merge(
            df_m, left_on="marmita_id", right_on="id", suffixes=('', '_m')
        )

        st.subheader("📊 Resumo Geral")
        faturamento_total = df_vendas["valor_total"].sum()
        lucro_liquido_total = df_vendas["lucro_total"].sum()
        total_marmitas_vendidas = df_vendas["quantidade"].sum()
        total_clientes = (
            df_vendas["cliente"].replace("", pd.NA).dropna().nunique()
        )

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Faturamento Total", f"R$ {faturamento_total:.2f}")
        m2.metric("Lucro Líquido Real", f"R$ {lucro_liquido_total:.2f}")
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
          st.plotly_chart(fig_dia, use_container_width=True)

        with col_g2:
          st.subheader("🍲 Marmitas Mais Vendidas (Quantidade)")
          vendas_por_prato = (
              df_vendas.groupby("nome")["quantidade"].sum().reset_index()
          )
          fig_prato = px.pie(
              vendas_por_prato,
              names="nome",
              values="quantidade",
              hole=0.4,
          )
          st.plotly_chart(fig_prato, use_container_width=True)

        st.divider()
        st.subheader("📋 Histórico Detalhado de Vendas e Exclusão")

        vendas_dict = {
            f"ID {r['id']} | Data: {r['data']} | {r['nome']} ({r['quantidade']} un)": r[
                'id'
            ]
            for _, r in df_vendas.iterrows()
        }

        col_ex_v1, col_ex_v2 = st.columns([2, 1])
        with col_ex_v1:
          tabela_exibicao = df_vendas[[
              "id",
              "data",
              "nome",
              "quantidade",
              "valor_unitario",
              "valor_total",
              "lucro_total",
              "cliente",
          ]]
          tabela_exibicao.columns = [
              "ID",
              "Data",
              "Marmita",
              "Qtd",
              "Preço Un. (R$)",
              "Total (R$)",
              "Lucro (R$)",
              "Cliente",
          ]
          tabela_exibicao = tabela_exibicao.round(2)
          st.dataframe(tabela_exibicao, use_container_width=True, hide_index=True)

        with col_ex_v2:
          st.write("🗑️ **Apagar Venda**")
          venda_selecionada = st.selectbox(
              "Selecione a venda", list(vendas_dict.keys())
          )
          if st.button("Excluir Venda Selecionada", type="primary"):
            id_venda_del = vendas_dict[venda_selecionada]
            try:
              client = get_turso_client()
              client.execute("DELETE FROM vendas WHERE id = ?", [id_venda_del])
              client.close()
              st.success("Venda excluída com sucesso!")
              st.rerun()
            except Exception as e:
              st.error(f"Erro ao excluir venda: {e}")

      else:
        st.info(
            "Nenhuma venda registrada ainda. Use o formulário acima para registrar a primeira venda!"
        )

  except Exception as e:
    st.error(f"Erro ao carregar o financeiro: {e}")
