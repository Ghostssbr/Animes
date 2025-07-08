from flask import Flask, jsonify, request, abort, redirect
from bs4 import BeautifulSoup
import cloudscraper
import concurrent.futures
import base64
import re
import requests
import time
import os

app = Flask(__name__)
scraper = cloudscraper.create_scraper(browser={
    'browser': 'chrome',
    'platform': 'android',
    'mobile': True
})

BASE_URL = "https://animefire.plus"
SECRET_KEY = "chave_ultra_segura"

cached_lancamentos = []
cached_atualizados = []

# ---------------- UTILIDADES ----------------

def gerar_token(titulo, link):
    ts = int(time.time())
    raw = f"{titulo}|{link}|{ts}"
    return base64.urlsafe_b64encode(raw.encode()).decode()

def decodificar_token(token):
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
        titulo, link, ts = raw.split("|")
        if time.time() - int(ts) > 600:
            return None, None
        return titulo, link
    except:
        return None, None

def get_with_retry(url, retries=3):
    for i in range(retries):
        try:
            time.sleep(1.5)
            res = scraper.get(url, timeout=15)
            res.raise_for_status()
            return res
        except Exception as e:
            print(f"❌ Tentativa {i+1} falhou em {url}: {e}")
            time.sleep(2)
    return None

# ---------------- SCRAPING ----------------

def scrape_animefire_page(page, section):
    url = f"{BASE_URL}/{section}/{page}"
    print(f"📄 Scraping página {page} da seção '{section}': {url}")
    
    res = get_with_retry(url)
    if not res:
        print(f"❌ Erro scraping {url}")
        return []
    
    soup = BeautifulSoup(res.text, 'html.parser')
    cards = soup.select("div.divCardUltimosEps")
    animes = []

    for card in cards:
        try:
            a = card.find("a")
            img = card.find("img")
            title = card.select_one("h3.animeTitle").text.strip()
            link = a['href']
            image = img.get("data-src") or img.get("src")
            print(f"✅ Anime extraído: {title}")
            animes.append({
                "title": title,
                "url": link,
                "image": image,
            })
        except Exception as e:
            print(f"⚠️ Erro processando card: {e}")
            continue

    return animes

def scrape_all_animes(section, total_pages):
    animes = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(scrape_animefire_page, p, section) for p in range(1, total_pages + 1)]
        for future in concurrent.futures.as_completed(futures):
            animes.extend(future.result())

    seen = set()
    unique = []
    for a in animes:
        if a['url'] not in seen:
            seen.add(a['url'])
            unique.append(a)
    
    for i, anime in enumerate(unique, 1):
        anime['id'] = i

    return unique

def scrape_episodes(anime_url):
    print(f"🎬 Scraping episódios de: {anime_url}")
    
    res = get_with_retry(anime_url)
    if not res:
        return {}

    soup = BeautifulSoup(res.text, 'html.parser')
    anime_info_div = soup.select_one("div.col-lg-9.text-white.divDivAnimeInfo")
    capa_anime = ""

    if anime_info_div:
        img_tag = anime_info_div.select_one("div.sub_animepage_img img")
        if img_tag:
            capa_anime = img_tag.get("data-src") or img_tag.get("src") or ""

    eps = []
    ep_links = soup.select("div.div_video_list a.lEp")

    for a in ep_links:
        link = a.get('href')
        titulo = a.text.strip()
        if not link.startswith("http"):
            link = BASE_URL + link

        capa = capa_anime

        try:
            ep_res = get_with_retry(link)
            if ep_res:
                ep_soup = BeautifulSoup(ep_res.text, 'html.parser')
                meta_thumb = ep_soup.select_one("meta[itemprop=thumbnailUrl]")
                if meta_thumb:
                    capa = meta_thumb.get("content", capa)
        except Exception as e:
            print(f"⚠️ Erro ao pegar capa do episódio {link}: {e}")

        print(f"📺 Episódio extraído: {titulo}")
        eps.append({
            "titulo": titulo,
            "link": link,
            "capa": capa
        })

    # Organizar por temporada
    seasons = {}
    for ep in eps:
        title_lower = ep['titulo'].lower()
        season_key = "1"
        m = re.search(r'(season|temporada|s)(\s?)(\d+)', title_lower)
        if m:
            season_key = m.group(3)

        seasons.setdefault(season_key, []).append(ep)

    for season in seasons:
        seasons[season].sort(key=lambda e: int(''.join(filter(str.isdigit, e['titulo'])) or 0))
        print(f"📦 Temporada {season}: {len(seasons[season])} episódios")

    return seasons

def scrape_mp4(ep_link):
    print(f"🎥 Buscando .mp4 do episódio: {ep_link}")
    res = get_with_retry(ep_link)
    if not res:
        return None
    match = re.search(r'"file"\s*:\s*"([^"]+\.mp4)"', res.text)
    if match:
        url = match.group(1).replace('\\/', '/')
        print(f"🎞️ Link .mp4 encontrado: {url}")
        return url
    print("❌ Nenhum link .mp4 encontrado.")
    return None

def fetch_anilist_info(title):
    print(f"🔎 Buscando dados do AniList: {title}")
    query = '''
    query ($search: String) {
        Media(search: $search, type: ANIME) {
            id
            title {
                romaji
                english
                native
            }
            description(asHtml: false)
            coverImage {
                large
            }
            episodes
            genres
            season
            seasonYear
            averageScore
            studios {
                nodes {
                    name
                }
            }
            trailer {
                id
                site
                thumbnail
            }
        }
    }'''
    variables = {"search": title}
    try:
        resp = requests.post("https://graphql.anilist.co", json={"query": query, "variables": variables}, timeout=8)
        resp.raise_for_status()
        return resp.json().get("data", {}).get("Media")
    except Exception as e:
        print(f"⚠️ Erro AniList API: {e}")
        return None

# ---------------- ROTAS ----------------

@app.route("/phantom/<token>")
def phantom(token):
    titulo, ep_link = decodificar_token(token)
    if not ep_link:
        return abort(403, "Token expirado ou inválido")
    
    mp4 = scrape_mp4(ep_link)
    if not mp4:
        return abort(404, "Conteúdo não encontrado")
    
    return redirect(mp4)

@app.route("/vault")
def vault():
    anime_id = request.args.get("id")
    if not anime_id:
        return abort(400, "Falta o parâmetro 'id'")
    
    all_animes = cached_lancamentos + cached_atualizados
    anime = next((a for a in all_animes if str(a['id']) == str(anime_id)), None)
    if not anime:
        return abort(404, "Anime não encontrado")

    print(f"🔍 Consultando detalhes do anime ID {anime['id']}: {anime['title']}")
    anilist = fetch_anilist_info(anime["title"])
    episodes = scrape_episodes(anime["url"])

    for season in episodes:
        for ep in episodes[season]:
            token = gerar_token(anime['title'], ep['link'])
            ep["player"] = request.host_url.rstrip("/") + f"/phantom/{token}"
            print(f"🔗 Player gerado para episódio: {ep['titulo']}")

    return jsonify({
        "id": anime["id"],
        "title": anime["title"],
        "image": anime["image"],
        "anilist": anilist,
        "episodes": episodes
    })

@app.route("/refresh")
def refresh():
    global cached_lancamentos, cached_atualizados
    print("🔁 Atualizando cache de lançamentos...")
    cached_lancamentos = scrape_all_animes("em-lancamento", 6)
    print("🔁 Atualizando cache de atualizados...")
    cached_atualizados = scrape_all_animes("animes-atualizados", 30)
    return jsonify({"status": "Cache atualizado com sucesso!"})

@app.route("/Releases")
def releases():
    print("🗒️ Retornando lista de lançamentos (/Releases)")
    return jsonify(cached_lancamentos)

@app.route("/updated")
def updated():
    print("🗒️ Retornando lista de atualizados (/updated)")
    return jsonify(cached_atualizados)

@app.route("/echo")
def echo():
    return jsonify({"ghost": "online"})

# ---------------- EXECUÇÃO ----------------

if __name__ == "__main__":
    print("🔄 Inicializando cache localmente...")
    cached_lancamentos = scrape_all_animes("em-lancamento", 6)
    cached_atualizados = scrape_all_animes("animes-atualizados", 30)
    print("✅ Cache pronto.")
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, threaded=True)
