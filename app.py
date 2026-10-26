import streamlit as st
import fitz  # PyMuPDF
from PIL import Image
import io
import json
import os
import re

# Soru_Kesici çekirdek fonksiyonlarını içe aktaralım veya tanımlayalım
# Not: Soru_Kesici_Z_Kitap_18x_Gmx.py dosyanız aynı klasörde olmalıdır.
try:
    import Soru_Kesici_Z_Kitap_18x_Gmx as core
except ImportError:
    core = None

st.set_page_config(page_title="PDF Soru Kesici (Web)", layout="wide", initial_sidebar_state="expanded")

st.markdown("""
<style>
    .main-title { font-size: 28px; font-weight: bold; color: #1e88e5; margin-bottom: 10px; }
    .stButton>button { width: 100%; border-radius: 6px; }
</style>
""", unsafe_allow_html=True)

st.markdown('<div class="main-title">✂️ PDF Soru Kesici & Z-Kitap Web</div>', unsafe_allow_html=True)

if "tum_kutular" not in st.session_state:
    st.session_state.tum_kutular = {}
if "pdf_bytes" not in st.session_state:
    st.session_state.pdf_bytes = None
if "toplam_sayfa" not in st.session_state:
    st.session_state.toplam_sayfa = 0

# --- SIDEBAR ---
with st.sidebar:
    st.header("⚙️ PDF & Ayarlar")
    yuklenen_pdf = st.file_uploader("Bir PDF dosyası seçin", type=["pdf"])
    
    if yuklenen_pdf:
        st.session_state.pdf_bytes = yuklenen_pdf.read()
        doc_temp = fitz.open(stream=st.session_state.pdf_bytes, filetype="pdf")
        st.session_state.toplam_sayfa = len(doc_temp)
        doc_temp.close()

    secenekli_sart = st.checkbox("Sadece şıklı (A-E) sorular", value=True)
    etkinlik_bul = st.checkbox("Etkinlik blokları", value=True)
    
    st.markdown("---")
    if st.session_state.pdf_bytes:
        aktif_sayfa_no = st.number_input("Sayfa Seçin", min_value=1, max_value=st.session_state.toplam_sayfa, value=1)
        sayfa_idx = aktif_sayfa_no - 1
    else:
        st.info("İşlem yapmak için sol taraftan bir PDF yükleyin.")
        st.stop()

# --- PDF İŞLEME ---
doc = fitz.open(stream=st.session_state.pdf_bytes, filetype="pdf")
page = doc.load_page(sayfa_idx)

col_sol, col_sag = st.columns([2.5, 1.5])

with col_sag:
    st.subheader("🎯 Soru Yönetimi")
    
    # Otomatik Tarama Butonu
    if st.button("🔍 Bu Sayfayı Otomatik Tara"):
        if core:
            with st.spinner("Sorular tespit ediliyor..."):
                kel = core.sayfa_kelimeleri(page)
                bulunanlar = core.sorulari_bul(page, 1.0, secenekli_sart=secenekli_sart, etkinlik=etkinlik_bul, kel=kel)
                kutular = []
                for q in bulunanlar:
                    kayit = {
                        'q_num': q['q_num'],
                        'bbox_pdf': list(q['bbox_pdf']),
                        'text': q['text']
                    }
                    if q.get('cevap'): kayit['cevap'] = q['cevap']
                    if q.get('ortak_rect'): kayit['ortak_rect'] = list(q['ortak_rect'])
                    kutular.append(kayit)
                # Soru numarasına göre sırala
                kutular.sort(key=lambda k: k.get('q_num', 0))
                st.session_state.tum_kutular[sayfa_idx] = kutular
                st.success(f"{len(kutular)} soru bulundu!")
        else:
            st.error("Soru_Kesici_Z_Kitap_18x_Gmx.py dosyası bulunamadı!")

    st.markdown("---")
    st.markdown("##### ➕ Araya / Yeni Soru Ekle")
    
    mevcut_kutular = st.session_state.tum_kutular.get(sayfa_idx, [])
    varsayilan_no = (max([k['q_num'] for k in mevcut_kutular], default=0) + 1)
    
    c1, c2 = st.columns(2)
    with c1:
        yeni_q_num = st.number_input("Soru No", min_value=1, max_value=999, value=varsayilan_no)
    with c2:
        yeni_cevap = st.selectbox("Cevap", ["-", "A", "B", "C", "D", "E"])

    # Koordinat Seçimi / Varsayılan
    st.caption("Soru Kırpma Alanı (PDF Koordinatları)")
    W, H = page.rect.width, page.rect.height
    cx1 = st.slider("Sol (X1)", 0.0, float(W), float(W * 0.05))
    cy1 = st.slider("Üst (Y1)", 0.0, float(H), float(H * 0.10))
    cx2 = st.slider("Sağ (X2)", 0.0, float(W), float(W * 0.48))
    cy2 = st.slider("Alt (Y2)", 0.0, float(H), float(H * 0.40))
    
    if st.button("➕ Soruyu Listeye / Araya Ekle"):
        # Araya ekleme: Eğer bu numara zaten varsa sonrakileri 1 ötele
        for k in mevcut_kutular:
            if k['q_num'] >= yeni_q_num:
                k['q_num'] += 1
                if k.get('text', '').endswith('.'):
                    k['text'] = f"{k['q_num']}."
        
        yeni_kutu = {
            'q_num': int(yeni_q_num),
            'bbox_pdf': [round(cx1, 1), round(cy1, 1), round(cx2, 1), round(cy2, 1)],
            'text': f"{yeni_q_num}.",
            'cevap': yeni_cevap if yeni_cevap != "-" else ""
        }
        mevcut_kutular.append(yeni_kutu)
        mevcut_kutular.sort(key=lambda k: k['q_num'])
        st.session_state.tum_kutular[sayfa_idx] = mevcut_kutular
        st.success(f"{yeni_q_num}. soru eklendi ve sıralama güncellendi.")
        st.rerun()

    # Mevcut Soruları Listele ve Silme
    if mevcut_kutular:
        st.markdown("---")
        st.markdown(f"##### 📋 Sayfadaki Sorular ({len(mevcut_kutular)})")
        for idx, k in enumerate(mevcut_kutular):
            satir = f"**Soru {k['q_num']}** - Cevap: `{k.get('cevap', '-')}` - Konum: `{k['bbox_pdf']}`"
            st.markdown(satir)

        if st.button("🗑️ Sayfadaki Soruları Temizle"):
            st.session_state.tum_kutular[sayfa_idx] = []
            st.rerun()

    # JSON İndirme
    st.markdown("---")
    tum_kayit = {
        'sayfalar': [
            {'sayfa': p + 1, 'kutular': kutular} 
            for p, kutular in sorted(st.session_state.tum_kutular.items()) if kutular
        ]
    }
    st.download_button(
        "💾 soru_kutulari.json İndir",
        data=json.dumps(tum_kayit, ensure_ascii=False, indent=2),
        file_name="soru_kutulari.json",
        mime="application/json"
    )

with col_sol:
    st.subheader(f"📄 Sayfa {sayfa_idx + 1} Görünümü")
    
    # Çizimleri ekrana yansıtarak görselleştir
    pix = page.get_pixmap(dpi=150)
    img = Image.open(io.BytesIO(pix.tobytes("png")))
    
    from PIL import ImageDraw, ImageFont
    draw = ImageDraw.Draw(img)
    scale = 150.0 / 72.0  # PDF koordinatından piksele dönüşüm

    for k in st.session_state.tum_kutular.get(sayfa_idx, []):
        bb = [v * scale for v in k['bbox_pdf']]
        draw.rectangle(bb, outline="red", width=3)
        draw.text((bb[0] + 5, bb[1] + 5), f"{k['q_num']}. ({k.get('cevap', '-')})", fill="red")
        
    st.image(img, use_container_width=True)

doc.close()
