import io
import time

import pandas as pd
import requests
import streamlit as st
from requests.auth import HTTPBasicAuth


st.set_page_config(page_title="WordPressレシピ登録", page_icon="📝", layout="wide")


def secret(name: str) -> str:
    value = st.secrets.get(name, "")
    return str(value).strip()


def api_request(method: str, path: str, **kwargs):
    base = secret("WP_URL").rstrip("/")
    password = secret("WP_APP_PASSWORD").replace(" ", "")
    auth = HTTPBasicAuth(secret("WP_USERNAME"), password)
    return requests.request(method, f"{base}{path}", auth=auth, timeout=30, **kwargs)


@st.cache_data(ttl=300, show_spinner=False)
def discover_recipe_endpoint():
    response = api_request("GET", "/wp-json/wp/v2/types")
    if response.status_code == 403:
        return "recipe", {"name": "レシピ（推定）"}, "投稿種類の一覧は403で保護されているため、recipeへ直接テストします。"
    response.raise_for_status()
    types = response.json()
    for key, item in types.items():
        labels = item.get("labels", {})
        text = " ".join([key, item.get("name", ""), labels.get("singular_name", "")]).lower()
        if key == "recipe" or "recipe" in text or "レシピ" in text:
            return item.get("rest_base") or key, item, ""
    raise RuntimeError("REST APIで『レシピ』の投稿タイプが見つかりませんでした。")


def clean(value):
    if pd.isna(value):
        return ""
    return str(value).replace("\ue000", "").strip()


def payload_from_row(row):
    payload = {
        "title": clean(row.get("Title")),
        "content": clean(row.get("Content")),
        "excerpt": clean(row.get("Excerpt")),
        "status": "draft",
    }
    for csv_name, wp_name in [("Slug", "slug"), ("Date", "date")]:
        value = clean(row.get(csv_name))
        if value:
            payload[wp_name] = value
    return payload


def find_by_slug(endpoint, slug):
    if not slug:
        return []
    response = api_request("GET", f"/wp-json/wp/v2/{endpoint}", params={"slug": slug, "context": "edit"})
    response.raise_for_status()
    return response.json()


def create_draft(endpoint, row):
    payload = payload_from_row(row)
    if not payload["title"] or not payload["content"]:
        raise ValueError("タイトルまたは本文が空欄です。")
    existing = find_by_slug(endpoint, payload.get("slug", ""))
    if existing:
        return "skip", existing[0].get("id"), "同じスラッグの記事があります"
    response = api_request("POST", f"/wp-json/wp/v2/{endpoint}", json=payload)
    if not response.ok:
        try:
            detail = response.json().get("message", response.text)
        except ValueError:
            detail = response.text
        raise RuntimeError(f"WordPressエラー {response.status_code}: {detail}")
    data = response.json()
    return "created", data.get("id"), data.get("link", "")


st.title("WordPress レシピ下書き登録")
st.caption("CSVを確認し、最初に1件だけテストしてから残りを登録します。公開は行いません。")

required = ["WP_URL", "WP_USERNAME", "WP_APP_PASSWORD", "APP_PASSWORD"]
missing = [name for name in required if not secret(name)]
if missing:
    st.error("StreamlitのSecretsが未設定です：" + "、".join(missing))
    st.stop()

if not st.session_state.get("authenticated"):
    entered = st.text_input("このアプリ専用のパスワード", type="password")
    if st.button("ログイン", type="primary"):
        if entered == secret("APP_PASSWORD"):
            st.session_state.authenticated = True
            st.rerun()
        else:
            st.error("パスワードが違います。")
    st.stop()

uploaded = st.file_uploader("レシピCSVを選択", type=["csv"])
if uploaded is None:
    st.info("CSVファイルを選択してください。")
    st.stop()

raw = uploaded.getvalue()
try:
    df = pd.read_csv(io.BytesIO(raw), encoding="utf-8-sig", dtype=str).fillna("")
except Exception as exc:
    st.error(f"CSVを読み込めませんでした：{exc}")
    st.stop()

needed = {"Title", "Content", "Slug"}
if not needed.issubset(df.columns):
    st.error("必要な列がありません：" + "、".join(sorted(needed - set(df.columns))))
    st.stop()

df["Title"] = df["Title"].map(clean)
st.success(f"{len(df)}件を読み込みました。登録状態はすべて『下書き』に固定します。")
st.dataframe(df[[c for c in ["Title", "Date", "季節", "種類", "Slug"] if c in df.columns]], use_container_width=True)

try:
    endpoint, type_info, endpoint_note = discover_recipe_endpoint()
    st.caption(f"接続先：{type_info.get('name', 'レシピ')}（REST API: {endpoint}）")
    if endpoint_note:
        st.warning(endpoint_note)
except Exception as exc:
    st.error(f"WordPressへ接続できません：{exc}")
    st.stop()

st.warning("現在の版では『季節』『種類』は確認表示のみです。WordPress側の分類API名を確認後に割り当てます。")

if st.button("先頭の1件だけテスト登録", type="primary"):
    try:
        status, post_id, message = create_draft(endpoint, df.iloc[0])
        if status == "created":
            st.session_state.test_ok = True
            st.success(f"テスト登録に成功しました。記事ID：{post_id}")
        else:
            st.session_state.test_ok = True
            st.info(f"先頭の記事は登録済みです。記事ID：{post_id}")
    except Exception as exc:
        st.session_state.test_ok = False
        st.error(str(exc))

confirm = st.checkbox("テスト結果をWordPressで確認しました。残りも下書き登録します。")
if st.button("残りをすべて下書き登録", disabled=not (st.session_state.get("test_ok") and confirm)):
    progress = st.progress(0)
    results = []
    for index, row in df.iterrows():
        try:
            status, post_id, message = create_draft(endpoint, row)
            results.append({"タイトル": row["Title"], "結果": "登録" if status == "created" else "スキップ", "記事ID": post_id, "詳細": message})
        except Exception as exc:
            results.append({"タイトル": row["Title"], "結果": "エラー", "記事ID": "", "詳細": str(exc)})
        progress.progress((index + 1) / len(df))
        time.sleep(0.15)
    result_df = pd.DataFrame(results)
    st.session_state.results = result_df

if "results" in st.session_state:
    result_df = st.session_state.results
    st.subheader("登録結果")
    st.dataframe(result_df, use_container_width=True)
    st.download_button("結果CSVをダウンロード", result_df.to_csv(index=False).encode("utf-8-sig"), "wordpress_import_result.csv", "text/csv")
