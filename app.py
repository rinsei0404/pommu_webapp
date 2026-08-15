import streamlit as st
import pandas as pd
import requests
import cloudscraper
import json
import time
import os
import random
import re
import io
import urllib.parse
from collections import defaultdict
from datetime import datetime, timedelta

from janome.tokenizer import Tokenizer
from wordcloud import WordCloud
import matplotlib.pyplot as plt
from PIL import Image, ImageDraw, ImageFont
from supabase import create_client, Client

# ---------------------------------------------
# 共通設定・Supabase接続
# ---------------------------------------------
# 💡 普通のブラウザからのアクセスに見せかける強力なヘッダー
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    "Referer": "https://ch.dlsite.com/",
    "Origin": "https://ch.dlsite.com"
}
DEFAULT_ICON_URL = "https://placehold.jp/150x150.png?text=No%20Image"
FONT_PATH = "ipaexg.ttf" 

# 💡 Cloudflare回避用のスクレイパーを作成
scraper = cloudscraper.create_scraper(browser={'browser': 'chrome', 'platform': 'windows', 'mobile': False})

try:
    SUPABASE_URL = st.secrets["SUPABASE_URL"]
    SUPABASE_KEY = st.secrets["SUPABASE_KEY"]
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
except Exception:
    supabase = None

def download_from_supabase(filename):
    if not supabase: return False
    if os.path.exists(filename): return True
    try:
        res = supabase.storage.from_("pommu-data").download(filename)
        with open(filename, "wb") as f: f.write(res)
        return True
    except: return False

def upload_to_supabase(filename):
    if not supabase: return
    try: supabase.storage.from_("pommu-data").remove([filename])
    except: pass
    try:
        with open(filename, "rb") as f:
            supabase.storage.from_("pommu-data").upload(
                file=f, path=filename, file_options={"content-type": "application/json"}
            )
    except: pass

def wrap_japanese_text(text, width=22):
    text = text.replace('\n', ' ')
    result = []
    for i in range(0, len(text), width): result.append(text[i:i+width])
    return '\n'.join(result)

# ---------------------------------------------
# データ取得・更新用関数（scraperに置換）
# ---------------------------------------------
@st.cache_data
def fetch_user_profile(user_id):
    try:
        res = scraper.get(f"https://ch.dlsite.com/api/pommu/users/{user_id}", headers=HEADERS)
        if res.status_code == 200:
            data = res.json()
            name, icon = None, None
            if "accountName" in data:
                name = data["accountName"]
                icon = data.get("profileImageUrl")
            elif "user" in data and "accountName" in data["user"]:
                name = data["user"]["accountName"]
                icon = data["user"].get("profileImageUrl")
            return {"name": name, "icon": icon}
    except: pass
    return None

@st.cache_data
def fetch_post_info(post_id):
    try:
        time.sleep(0.3)
        res = scraper.get(f"https://ch.dlsite.com/api/pommu/posts/{post_id}", headers=HEADERS)
        if res.status_code == 200: return res.json()
    except: pass
    return None

def fetch_and_update_posts(user_id):
    filename = f"{user_id}_posts.json"
    analysis_file = f"{user_id}_analysis.json"
    
    download_from_supabase(filename)
    download_from_supabase(analysis_file)
    
    existing_posts = []
    if os.path.exists(filename):
        with open(filename, "r", encoding="utf-8") as f: existing_posts = json.load(f)
            
    latest_existing_id = existing_posts[0].get("id") if existing_posts and isinstance(existing_posts[0], dict) else None
    
    first_url = f"https://ch.dlsite.com/api/pommu/users/{user_id}/posts?ageCategory=2&limit=30&page=1"
    response = scraper.get(first_url, headers=HEADERS)
    if response.status_code != 200:
        st.error(f"APIからのデータ取得に失敗しました。（エラーコード: {response.status_code}）")
        return False
        
    data = response.json()
    first_page_posts = data.get("posts", [])
    last_page = data.get("pagination", {}).get("lastPage", 1)
    
    if not existing_posts:
        st.info("初回起動のため、全件を取得します...")
        all_posts = []
        bar = st.progress(0)
        progress_text = st.empty()
        for page in range(1, last_page + 1):
            progress_text.text(f"📥 投稿を取得中... ({page}/{last_page}ページ)")
            res = scraper.get(f"https://ch.dlsite.com/api/pommu/users/{user_id}/posts?ageCategory=2&limit=30&page={page}", headers=HEADERS)
            if res.status_code == 200: all_posts.extend(res.json().get("posts", []))
            bar.progress(page / last_page)
            time.sleep(1)
            
        with open(filename, "w", encoding="utf-8") as f: json.dump(all_posts, f, ensure_ascii=False, indent=2)
        upload_to_supabase(filename)
        st.success(f"✅ 全 {len(all_posts)} 件の保存が完了しました！")
        return True

    new_posts = []
    for p in first_page_posts:
        if isinstance(p, dict) and p.get("id") == latest_existing_id: break
        new_posts.append(p)
        
    if new_posts:
        st.success(f"✨ 新しい投稿が **{len(new_posts)}件** 見つかりました！差分を追加・集計します。")
        with open(filename, "w", encoding="utf-8") as f: json.dump(new_posts + existing_posts, f, ensure_ascii=False, indent=2)
        upload_to_supabase(filename)
        
        if os.path.exists(analysis_file):
            st.info("🔄 分析データを差分アップデートしています...")
            with open(analysis_file, "r", encoding="utf-8") as f:
                saved = json.load(f)
                u_scores = defaultdict(int, saved.get("user_scores", {}))
                r_scores = defaultdict(int, saved.get("recent_scores", {}))
                u_info = saved.get("user_info", {})
                
            thirty_days_ago = datetime.now() - timedelta(days=30)
            bar = st.progress(0)
            for i, post in enumerate(new_posts):
                if "deletedAt" in post: continue
                post_date = datetime.strptime(post["createdAt"], "%Y-%m-%d %H:%M:%S") if post.get("createdAt") else None
                for target_id, weight in [(post.get("replyToId"), 3), (post.get("quotedPostId"), 2)]:
                    if target_id:
                        info = fetch_post_info(target_id)
                        if info and "user" in info:
                            uid = str(info["user"]["accountId"])
                            if uid == str(user_id): continue 
                            u_scores[uid] += weight
                            if post_date and post_date >= thirty_days_ago: r_scores[uid] += weight
                            icon_url = info["user"].get("profileImageUrl")
                            u_info[uid] = {"name": info["user"]["accountName"], "icon": icon_url if icon_url and not icon_url.startswith("data:") else DEFAULT_ICON_URL}
                bar.progress((i + 1) / len(new_posts))
                
            with open(analysis_file, "w", encoding="utf-8") as f:
                json.dump({"user_scores": dict(u_scores), "recent_scores": dict(r_scores), "user_info": u_info}, f, ensure_ascii=False)
            upload_to_supabase(analysis_file)
            st.success("✅ 分析データの差分アップデートが完了しました！")
    else:
        st.info("💡 新しい投稿はありませんでした。最新の状態です！")
    return True

def get_pil_image_from_url(url, size=(100, 100)):
    try:
        res = scraper.get(url, headers=HEADERS, timeout=3)
        if res.status_code == 200: return Image.open(io.BytesIO(res.content)).convert("RGBA").resize(size)
    except: pass
    return Image.new("RGBA", size, (200, 200, 200, 255))

# ---------------------------------------------
# メイン画面レイアウト
# ---------------------------------------------
st.title("私のえごったー（仮）")
input_user_id = st.text_input("あなたのPommuユーザーIDを入力してください（例: 181734）")
posts_file = f"{input_user_id}_posts.json" if input_user_id else ""
analysis_file = f"{input_user_id}_analysis.json" if input_user_id else ""

if "is_analyzing" not in st.session_state: st.session_state.is_analyzing = False

if input_user_id:
    st.header("1. データの準備")
    
    if not os.path.exists(posts_file):
        with st.spinner("☁️ クラウドからデータを検索中..."):
            download_from_supabase(posts_file)
            download_from_supabase(analysis_file)

    if os.path.exists(posts_file): st.success("✅ 投稿データは保存されています。")
    else: st.warning("⚠️ 投稿データがありません。「最新状態に更新」ボタンを押してください。")
        
    if st.button("🔄 最新状態に更新（差分チェック）"): fetch_and_update_posts(input_user_id)
    st.write("---")
    st.header("2. 分析メニュー")

    if st.button("⚡ 保存された分析結果を読み込む" if os.path.exists(analysis_file) else "📊 新規に集計して分析を開始する"):
        st.session_state.is_analyzing = True

    if st.session_state.is_analyzing:
        if not os.path.exists(posts_file):
            st.error("投稿データが見つかりません。先にデータを更新してください。")
        else:
            with open(posts_file, "r", encoding="utf-8") as f: posts = json.load(f)
            
            if os.path.exists(analysis_file):
                with open(analysis_file, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                    user_scores, recent_scores, user_info = saved["user_scores"], saved["recent_scores"], saved["user_info"]
            else:
                user_scores, recent_scores, user_info = defaultdict(int), defaultdict(int), {}
                thirty_days_ago = datetime.now() - timedelta(days=30)
                st.write("🔄 関係性を全件集計中...")
                bar = st.progress(0)
                for i, post in enumerate(posts):
                    if "deletedAt" in post: continue
                    post_date = datetime.strptime(post["createdAt"], "%Y-%m-%d %H:%M:%S") if post.get("createdAt") else None
                    for target_id, weight in [(post.get("replyToId"), 3), (post.get("quotedPostId"), 2)]:
                        if target_id:
                            info = fetch_post_info(target_id)
                            if info and "user" in info:
                                uid = str(info["user"]["accountId"])
                                if uid == str(input_user_id): continue 
                                user_scores[uid] += weight
                                if post_date and post_date >= thirty_days_ago: recent_scores[uid] += weight
                                icon_url = info["user"].get("profileImageUrl")
                                user_info[uid] = {"name": info["user"]["accountName"], "icon": icon_url if icon_url and not icon_url.startswith("data:") else DEFAULT_ICON_URL}
                    bar.progress((i + 1) / len(posts))
                    
                with open(analysis_file, "w", encoding="utf-8") as f:
                    json.dump({"user_scores": dict(user_scores), "recent_scores": dict(recent_scores), "user_info": user_info}, f, ensure_ascii=False)
                upload_to_supabase(analysis_file)

            st.subheader("🕒 活動時間の分析")
            view_mode = st.radio("表示形式", ["🔥 曜日×時間帯ヒートマップ", "📊 24時間トータル棒グラフ", "📈 月ごとの投稿推移"], index=1, horizontal=True)
            if view_mode == "🔥 曜日×時間帯ヒートマップ":
                h_data = [[0] * 24 for _ in range(7)]
                for p in posts:
                    if p.get("createdAt"): h_data[datetime.strptime(p["createdAt"], "%Y-%m-%d %H:%M:%S").weekday()][datetime.strptime(p["createdAt"], "%Y-%m-%d %H:%M:%S").hour] += 1
                st.dataframe(pd.DataFrame(h_data, index=["月", "火", "水", "木", "金", "土", "日"], columns=[f"{h:02d}時" for h in range(24)]).style.background_gradient(cmap="Purples"), use_container_width=True)
            elif view_mode == "📊 24時間トータル棒グラフ":
                h_total = [0] * 24
                for p in posts:
                    if p.get("createdAt"): h_total[datetime.strptime(p["createdAt"], "%Y-%m-%d %H:%M:%S").hour] += 1
                st.bar_chart(pd.DataFrame({"時間帯": [f"{h:02d}時" for h in range(24)], "投稿数": h_total}).set_index("時間帯")["投稿数"])
            else:
                m_counts = defaultdict(int)
                for p in posts:
                    if p.get("createdAt"): m_counts[datetime.strptime(p["createdAt"], "%Y-%m-%d %H:%M:%S").strftime("%Y-%m")] += 1
                st.bar_chart(pd.DataFrame(sorted(m_counts.items()), columns=["年月", "投稿数"]).set_index("年月")["投稿数"])

            st.subheader("👑 総合仲良しランキング")
            ranking = sorted(user_scores.items(), key=lambda x: x[1], reverse=True)[:10]
            st.dataframe(pd.DataFrame([{"順位": r, "アイコン": user_info[u]["icon"], "ユーザー名": user_info[u]["name"], "スコア": s} for r, (u, s) in enumerate(ranking, 1)]), column_config={"アイコン": st.column_config.ImageColumn(width="small")}, hide_index=True, use_container_width=True)

            st.subheader("🕰️ 初絡み")
            for p in sorted([p for p in posts if "deletedAt" not in p], key=lambda x: x.get("createdAt", "")):
                t_id = p.get("replyToId") or p.get("quotedPostId")
                if t_id:
                    t_info = fetch_post_info(t_id)
                    if t_info and str(t_info.get("user", {}).get("accountId")) != str(input_user_id):
                        st.success(f"🎉 初絡みは **{t_info['user']['accountName']}** さんでした！")
                        st.info(f"{p.get('createdAt')}\n\n{p.get('texts', '')}\n[🔗 投稿を見る](https://ch.dlsite.com/pommu/posts/{p['id']})")
                        break

            st.subheader("🔥 人気の投稿TOP3")
            for i, p in enumerate(sorted([p for p in posts if "deletedAt" not in p], key=lambda x: x.get("favoritedByCount", 0) + x.get("quotedByCount", 0), reverse=True)[:3]):
                st.markdown(f"**第{i+1}位** 👑 (いいね: {p.get('favoritedByCount',0)} / 引用: {p.get('quotedByCount',0)}) [🔗 投稿を見る](https://ch.dlsite.com/pommu/posts/{p['id']})")
                st.info(p.get("texts", ""))

            st.subheader("🕳️ 埋もれた投稿発掘")
            zero_posts = [p for p in posts if "deletedAt" not in p and p.get("favoritedByCount",0)==0 and p.get("quotedByCount",0)==0 and p.get("repliedByCount",0)==0 and not p.get("replyToId") and not p.get("quotedPostId")]
            if zero_posts:
                dp = random.choice(zero_posts)
                st.warning(f"⚠️ 発掘されました... (日時: {dp.get('createdAt', '')})")
                st.info(f"{dp.get('texts', '')}\n\n[🔗 過去を見に行く](https://ch.dlsite.com/pommu/posts/{dp['id']})")
            else: st.success("対象の投稿はありませんでした！")

            st.subheader("🏷️ よく使うハッシュタグ")
            h_counts = defaultdict(int)
            for p in posts:
                if "deletedAt" not in p and p.get("texts"):
                    for tag in re.findall(r'[#＃]([\wぁ-んァ-ヶ一-龠ー]+)', p["texts"]): h_counts[tag] += 1
            if h_counts: st.dataframe(pd.DataFrame(sorted(h_counts.items(), key=lambda x: x[1], reverse=True)[:10], columns=["ハッシュタグ", "使用回数"]), hide_index=True)

            st.subheader("☁️ あなたの脳内（ワードクラウド）")
            if st.button("ワードクラウドを生成する"):
                with st.spinner("言葉を分析中..."):
                    t, words = Tokenizer(), []
                    stop_words = {"の", "ん", "こと", "もの", "これ", "それ", "あれ", "よう", "さん", "ちゃん", "くん", "たち", "今日", "明日", "昨日", "リプライ"}
                    for p in posts:
                        if "deletedAt" not in p and p.get("texts"):
                            for token in t.tokenize(p["texts"]):
                                pos = token.part_of_speech.split(',')
                                if pos[0] in ['名詞', '形容詞'] and (len(pos) == 1 or pos[1] not in ['非自立', '代名詞', '数']):
                                    if token.base_form not in stop_words: words.append(token.base_form)
                    text_for_cloud = " ".join(words)
                    if text_for_cloud:
                        wc = WordCloud(font_path=FONT_PATH, width=800, height=400, background_color="white", colormap="viridis").generate(text_for_cloud)
                        fig, ax = plt.subplots(figsize=(10, 5)); ax.imshow(wc); ax.axis("off"); st.pyplot(fig)

            st.write("---")
            st.subheader("🖼️ 分析レポートを画像で書き出す")
            
            if st.button("✨ レポート画像を生成する"):
                with st.spinner("画像を生成中..."):
                    try:
                        W, H = 1280, 720
                        img = Image.new('RGB', (W, H), color=(245, 245, 240))
                        draw = ImageDraw.Draw(img)
                        
                        f_title = ImageFont.truetype(FONT_PATH, 42)
                        f_head = ImageFont.truetype(FONT_PATH, 32)
                        f_body = ImageFont.truetype(FONT_PATH, 24)
                        
                        my_profile = fetch_user_profile(input_user_id)
                        my_name = f"ユーザー {input_user_id}"
                        my_icon_url = DEFAULT_ICON_URL
                        
                        if my_profile and my_profile["name"]:
                            my_name = my_profile["name"]
                            if my_profile["icon"] and not my_profile["icon"].startswith("data:"):
                                my_icon_url = my_profile["icon"]
                        elif posts:
                            first_post_info = fetch_post_info(posts[0]["id"])
                            if first_post_info and "user" in first_post_info:
                                my_name = first_post_info["user"].get("accountName", my_name)
                                icon_url = first_post_info["user"].get("profileImageUrl")
                                if icon_url and not icon_url.startswith("data:"): my_icon_url = icon_url
                        
                        draw.rounded_rectangle([40, 100, 600, 680], radius=15, fill=(255, 255, 255), outline=(200, 200, 200), width=2)
                        draw.rounded_rectangle([640, 100, 1240, 360], radius=15, fill=(255, 255, 255), outline=(200, 200, 200), width=2)
                        draw.rounded_rectangle([640, 400, 1240, 680], radius=15, fill=(255, 255, 255), outline=(200, 200, 200), width=2)
                        
                        my_icon_img = get_pil_image_from_url(my_icon_url, size=(60, 60))
                        img.paste(my_icon_img, (50, 20))
                        draw.text((130, 25), f"{my_name} さん", font=f_title, fill=(50, 50, 50))
                        draw.text((950, 40), f"総投稿: {len(posts)} 件", font=f_title, fill=(50, 50, 50))
                        
                        draw.text((60, 120), "仲良しランキング", font=f_head, fill=(80, 80, 80))
                        for i, (u_id, score) in enumerate(ranking[:3]):
                            y_offset = 180 + (i * 160)
                            draw.text((60, y_offset + 30), f"{i+1}.", font=f_title, fill=(100, 100, 100))
                            icon_img = get_pil_image_from_url(user_info[u_id]['icon'], size=(100, 100))
                            img.paste(icon_img, (120, y_offset))
                            draw.text((240, y_offset + 10), user_info[u_id]['name'][:15], font=f_body, fill=(30, 30, 30))
                            draw.text((240, y_offset + 50), f"ID: {u_id}", font=f_body, fill=(30, 30, 30))

                        draw.text((660, 120), "初投稿", font=f_head, fill=(80, 80, 80))
                        oldest_post = sorted([p for p in posts if "deletedAt" not in p], key=lambda x: x.get("createdAt", ""))
                        if oldest_post:
                            oldest_text = oldest_post[0].get("texts", "")
                            oldest_date = oldest_post[0].get("createdAt", "")[:10]
                            wrapped_text = wrap_japanese_text(oldest_text, width=22)
                            lines = wrapped_text.split('\n')
                            if len(lines) > 4: wrapped_text = '\n'.join(lines[:4]) + " ..."
                            
                            draw.text((680, 180), f"「{wrapped_text}」", font=f_body, fill=(50, 50, 50))
                            draw.text((1050, 310), f"Date: {oldest_date}", font=f_body, fill=(120, 120, 120))

                        draw.text((660, 420), "くちぐせ", font=f_head, fill=(80, 80, 80))
                        if 'text_for_cloud' not in locals():
                            t_w, words_w = Tokenizer(), []
                            stop_w = {"の", "ん", "こと", "もの", "これ", "それ", "あれ", "よう", "さん", "ちゃん", "くん", "たち", "今日", "明日", "昨日", "リプライ"}
                            for p in posts:
                                if "deletedAt" not in p and p.get("texts"):
                                    for token in t_w.tokenize(p["texts"]):
                                        pos = token.part_of_speech.split(',')
                                        if pos[0] in ['名詞', '形容詞'] and (len(pos) == 1 or pos[1] not in ['非自立', '代名詞', '数']) and token.base_form not in stop_w:
                                            words_w.append(token.base_form)
                            text_for_cloud = " ".join(words_w)
                        
                        if text_for_cloud:
                            wc_img_obj = WordCloud(font_path=FONT_PATH, width=540, height=180, background_color="white", colormap="viridis").generate(text_for_cloud).to_image()
                            img.paste(wc_img_obj, (670, 470))
                            
                        draw.text((1100, 690), datetime.now().strftime("%Y/%m/%d"), font=f_body, fill=(150, 150, 150))
                        
                        buf = io.BytesIO()
                        img.save(buf, format="PNG")
                        img_bytes = buf.getvalue()
                        
                        st.image(img_bytes, caption="生成されたレポート画像", use_container_width=True)
                        st.download_button(label="📥 画像を保存する (PNG)", data=img_bytes, file_name=f"{input_user_id}_report.png", mime="image/png")
                        
                    except Exception as e:
                        st.error(f"画像生成エラー（フォントパス等を確認してください）: {e}")

            st.write("---")
            st.subheader("🌐 結果をシェアする")
            
            share_text = f"【Pommuえごったー】で分析しました！✨\n\n📝 総投稿数: {len(posts)}件\n\n↓あなたも分析してみる？\n"
            share_url = f"https://ch.dlsite.com/pommu/posts/create?text={urllib.parse.quote(share_text)}"
            
            st.info("画像を保存したら、以下のボタンからPommuに結果を投稿してみましょう！")
            st.link_button("🚀 Pommuでシェアする", share_url)
