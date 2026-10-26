import streamlit as st
import fitz  # PyMuPDF
from PIL import Image
import io
import json

# Sayfa yapılandırması
st.set_page_config(page_title="PDF Soru Kesici Web", layout="wide")

st.title("📄 PDF Soru Kesici & Z-Kitap Düzenleyici")

# Sol menü - Ayarlar ve Yükleme
with st.sidebar:
    st.header("Dosya ve Ayarlar")
    pdf_file = st.file_uploader("PDF Dosyası Yükleyin", type=["pdf"])
    secenekli_sart = st.checkbox("Sadece şıklı (A-E) sorular", value=True)
    etkinlik_bul = st.checkbox("Etkinlik blokları", value=True)

if pdf_file:
    # PDF'i bellekte aç
    pdf_bytes = pdf_file.read()
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    toplam_sayfa = len(doc)
    
    sayfa_no = st.sidebar.number_input("Sayfa Seçin", min_value=1, max_value=toplam_sayfa, value=1) - 1
    
    page = doc.load_page(sayfa_no)
    pix = page.get_pixmap(dpi=150)
    img = Image.open(io.BytesIO(pix.tobytes("png")))
    
    col1, col2 = st.columns([3, 1])
    
    with col1:
        st.subheader(f"Sayfa {sayfa_no + 1} Önizleme")
        st.image(img, use_column_width=True)
        # Not: Etkileşimli kutu çizimi için 'streamlit-drawable-canvas' bileşeni entegre edilir.

    with col2:
        st.subheader("İşlemler")
        if st.button("Sayfayı Tara ve Kutuları Bul"):
            st.info("Soru algılama algoritması çalıştırılıyor...")
            # Mevcut sorulari_bul(page, ...) fonksiyonunuz burada çağrılır
            st.success("Sorular tespit edildi!")
            
        st.divider()
        st.download_button(
            label="Kutuları JSON Olarak İndir",
            data=json.dumps({"sayfa": sayfa_no + 1, "kutular": []}, indent=2),
            file_name="soru_kutulari.json",
            mime="application/json"
        )