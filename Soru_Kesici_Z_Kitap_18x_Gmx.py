import os
import sys
import json
import re
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import fitz  # PyMuPDF
from PIL import Image, ImageChops, ImageTk

# ======================================================================
#  SORU TESPITI
# ======================================================================
DEBUG = True

NUMARA_RE = re.compile(r'^(\d{1,3})\.$')
SECENEK_RE = re.compile(r'^([A-E])\)')
UST_ORAN = 0.10
ALT_ORAN = 0.935
TOL = 6.0
MARJ_TOL = 9.0

MARJ_VARSAYILAN_TEK = {'ust': 0.02, 'alt': 0.98, 'sol': 0.02, 'sag': 0.98}
MARJ_VARSAYILAN_CIFT = {'ust': 0.02, 'alt': 0.98, 'sol': 0.02, 'sag': 0.98}
MARJ_BASLIK_VARSAYILAN_TEK = {'ust': 0.0, 'alt': 0.20, 'sol': 0.0, 'sag': 1.0}
MARJ_BASLIK_VARSAYILAN_CIFT = {'ust': 0.0, 'alt': 0.20, 'sol': 0.0, 'sag': 1.0}

_ocr = None

def _sayfa_tek_mi(sayfa_idx):
    return (sayfa_idx + 1) % 2 == 1

def pdf_sayfasini_goruntuye(pdf_doc, sayfa_no, dpi=150):
    page = pdf_doc.load_page(sayfa_no)
    zoom = dpi / 72.0
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    return img, zoom

def _ocr_motoru():
    global _ocr
    if _ocr is None:
        try:
            from paddleocr import PaddleOCR
        except ImportError:
            raise RuntimeError("PaddleOCR kurulu degil.")
        try:
            _ocr = PaddleOCR(use_textline_orientation=True, lang='tr',
                             use_doc_orientation_classify=False, use_doc_unwarping=False)
        except TypeError:
            _ocr = PaddleOCR(lang='tr')
    return _ocr

def ocr_kelimeleri(page, dpi=150):
    import numpy as np
    import cv2
    img, zoom = pdf_sayfasini_goruntuye(page.parent, page.number, dpi)
    img_cv = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
    result = _ocr_motoru().predict(img_cv)
    satirlar = []
    if result and isinstance(result[0], dict):
        for text, poly in zip(result[0].get('rec_texts', []), result[0].get('rec_polys', [])):
            xs = [float(p[0]) for p in poly]; ys = [float(p[1]) for p in poly]
            satirlar.append((text, min(xs), min(ys), max(xs), max(ys)))
    kel = []
    for text, x0, y0, x1, y1 in satirlar:
        tokens = text.split()
        toplam = max(1, sum(len(t) + 1 for t in tokens))
        imlec = 0
        for t in tokens:
            a = x0 + (x1 - x0) * imlec / toplam
            imlec += len(t) + 1
            b = x0 + (x1 - x0) * imlec / toplam
            kel.append((a / zoom, y0 / zoom, b / zoom, y1 / zoom, t))
    return kel

_SIFIR_GENISLIK = dict.fromkeys(map(ord, '\u200b\u200c\u200d\u2060\ufeff'), None)

def kelimeleri_normalle(kel):
    """Soru numarasi tespitini bozan metin katmani artefaktlarini duzeltir:
    gorunmez karakterler, '13' + '.' ayri gelmesi, '13.Bir' yapisik gelmesi."""
    cikis = []
    for x0, y0, x1, y1, t in kel:
        t = t.translate(_SIFIR_GENISLIK).replace('\u2024', '.')
        if not t.strip():
            continue
        m = re.match(r'^(\d{1,3})\.([A-Za-z\u00c7\u011e\u0130\u00d6\u015e\u00dc\u00e7\u011f\u0131\u00f6\u015f\u00fc].*)$', t)
        if m:
            xm = x0 + (x1 - x0) * (len(m.group(1)) + 1) / len(t)
            cikis.append((x0, y0, xm, y1, m.group(1) + '.'))
            cikis.append((xm, y0, x1, y1, m.group(2)))
            continue
        cikis.append((x0, y0, x1, y1, t))
    birlesik = []
    for k in cikis:
        if (birlesik and k[4] == '.' and re.fullmatch(r'\d{1,3}', birlesik[-1][4])
                and abs(k[1] - birlesik[-1][1]) < 3 and -1 <= k[0] - birlesik[-1][2] <= 2.5):
            p = birlesik.pop()
            birlesik.append((p[0], min(p[1], k[1]), k[2], max(p[3], k[3]), p[4] + '.'))
        else:
            birlesik.append(k)
    return birlesik

def sayfa_kelimeleri(page):
    kel = [(w[0], w[1], w[2], w[3], w[4]) for w in page.get_text("words") if w[4].strip()]
    if len(kel) < 15 and page.get_images():
        if DEBUG:
            print("  (metin katmani yok -> OCR)")
        return kelimeleri_normalle(ocr_kelimeleri(page))
    return kelimeleri_normalle(kel)

def _baslik_alt_sinir(kel, W, H):
    if not kel:
        return H * 0.04
    yasak_sonra = {'unite', 'ünite', 'test', 'bolum', 'bölüm',
                   'kitap', 'sinif', 'sınıf', 'konu', 'metin'}
    ust_kel = sorted([k for k in kel if k[1] < 0.22 * H], key=lambda k: (k[1], k[0]))
    for i, k in enumerate(ust_kel):
        if not NUMARA_RE.match(k[4]):
            continue
        sonra = None
        for k2 in ust_kel[i + 1:]:
            if k2[1] > k[3] + 3:
                break
            if k2[0] >= k[2] - 1:
                sonra = k2[4].lower()
                break
        if sonra in yasak_sonra:
            continue
        return max(H * 0.02, k[1] - 6)
    return H * 0.04

def _cevap_seridi_ust(kel, W, H, rects=None):
    if not kel:
        return None
    satirlar = {}
    for k in kel:
        if k[1] < 0.70 * H:
            continue
        satirlar.setdefault(round(k[1] / 6), []).append(k)
    bulunan = None
    for key in sorted(satirlar, reverse=True):
        s = sorted(satirlar[key], key=lambda k: k[0])
        if len(s) < 6:
            continue
        birlesik = [k for k in s if re.fullmatch(r'\d{1,3}[-–][A-E]', k[4])]
        if len(birlesik) >= 4:
            bulunan = min(k[1] for k in birlesik)
            if rects is not None:
                cevap_tok = [k for k in s if re.fullmatch(r'\d{1,3}[-–.]?', k[4])
                             or re.fullmatch(r'[A-E]\)?', k[4])]
                if cevap_tok:
                    rects.append(fitz.Rect(
                        min(k[0] for k in cevap_tok), min(k[1] for k in cevap_tok),
                        max(k[2] for k in cevap_tok), max(k[3] for k in cevap_tok)))
            continue
        ritim = "".join('S' if re.fullmatch(r'\d{1,3}[-–.]?', k[4]) else
                        'H' if re.fullmatch(r'[A-E]\)?', k[4]) else 'x' for k in s)
        ritim = ritim.replace('x', '')
        if len(ritim) >= 10 and ritim.count('SH') >= 4 and len(re.findall(r'S+', ritim)) >= 4:
            bulunan = min(k[1] for k in s if re.fullmatch(r'\d{1,3}[-–.]?', k[4]) or
                          re.fullmatch(r'[A-E]\)?', k[4]))
            if rects is not None:
                cevap_tok = [k for k in s if re.fullmatch(r'\d{1,3}[-–.]?', k[4])
                             or re.fullmatch(r'[A-E]\)?', k[4])]
                if cevap_tok:
                    rects.append(fitz.Rect(
                        min(k[0] for k in cevap_tok), min(k[1] for k in cevap_tok),
                        max(k[2] for k in cevap_tok), max(k[3] for k in cevap_tok)))
    return bulunan

def _dikey_metin_araliklari(kel, min_oran=2.5, min_yuk=15):
    araliklar = []
    for k in kel:
        x0, y0, x1, y1 = k[0], k[1], k[2], k[3]
        if x1 <= x0:
            continue
        if (y1 - y0) > min_oran * (x1 - x0) and (y1 - y0) > min_yuk:
            araliklar.append([x0, x1])
    if not araliklar:
        return []
    araliklar.sort()
    birlesik = [araliklar[0]]
    for a in araliklar[1:]:
        if a[0] <= birlesik[-1][1] + 3:
            birlesik[-1][1] = max(birlesik[-1][1], a[1])
        else:
            birlesik.append(a)
    return [(a[0], a[1]) for a in birlesik]

# ----------------------------------------------------------------------
#  ORTAK ALAN YARDIMCILARI (yonerge tespiti, gorsel/cizim rect'leri)
# ----------------------------------------------------------------------
YONERGE_RE = re.compile(r'a[s\u015f]a[g\u011f][\u0131i]da|yukar[\u0131i]da')

def _tr_kucuk(s):
    return s.replace('\u0130', 'i').replace('I', '\u0131').lower()

def _satirlara_bol(kel):
    sirali = sorted(kel, key=lambda k: ((k[1] + k[3]) / 2, k[0]))
    satirlar = []
    for k in sirali:
        yc = (k[1] + k[3]) / 2
        h = max(1.0, k[3] - k[1])
        if satirlar and abs(yc - satirlar[-1]['yc']) <= max(2.5, 0.45 * h):
            s = satirlar[-1]
            s['kel'].append(k)
            s['yc'] += (yc - s['yc']) / len(s['kel'])
        else:
            satirlar.append({'yc': yc, 'kel': [k]})
    return [s['kel'] for s in satirlar]

def _yonerge_parcalari(kel, orta):
    """Her satiri sutun bosluguna (gutter) gore boler. Boylece sol sutundaki bir
    yonerge ile ayni hizadaki sag sutun metni birlesip 'tam genislik' sanilmaz."""
    parcalar = []
    for satir in _satirlara_bol(kel):
        satir = sorted(satir, key=lambda k: k[0])
        sol = [k for k in satir if (k[0] + k[2]) / 2 < orta]
        sag = [k for k in satir if (k[0] + k[2]) / 2 >= orta]
        gruplar = [satir]
        if sol and sag:
            sol_son = max(k[2] for k in sol)
            sag_ilk = min(k[0] for k in sag)
            if sag_ilk - sol_son >= 9 and sol_son <= orta + 12 and sag_ilk >= orta - 12:
                gruplar = [sol, sag]
        for g in gruplar:
            x0, x1 = min(k[0] for k in g), max(k[2] for k in g)
            y0, y1 = min(k[1] for k in g), max(k[3] for k in g)
            if x1 <= orta + 20:
                kol = 0
            elif x0 >= orta - 20:
                kol = 1
            else:
                kol = None
            parcalar.append({'kol': kol, 'x0': x0, 'x1': x1, 'y0': y0, 'y1': y1,
                             'metin': " ".join(k[4] for k in g)})
    return parcalar

def _sayi_listesi(metin):
    sayilar = set()
    for m in re.finditer(r'(?<!\d)(\d{1,3})(?:\s*[-\u2013]\s*(\d{1,3}))?(?!\d)', metin):
        a = int(m.group(1))
        sayilar.add(a)
        if m.group(2):
            b = int(m.group(2))
            if a < b <= a + 15:
                sayilar.update(range(a, b + 1))
            else:
                sayilar.add(b)
    return sorted(sayilar)

SORU_KELIME_RE = re.compile(r'\bsoru(?:lar[\u0131i]?|y[ua]|su|da|dan|lar[\u0131i]n[\u0131i]?)?\b')

def _yonerge_soru_var(mk):
    """'3. ve 4. sorulari ...' gibi: GERCEK 'soru' kelimesi ve hemen oncesinde bir sayi.
    'sorumluluk', 'sorunlar' gibi kelimelerdeki 'soru' yonerge sayilmaz."""
    for m in SORU_KELIME_RE.finditer(mk):
        if re.search(r'\d{1,3}\s*[.)]?\s*$', mk[max(0, m.start() - 12):m.start()]):
            return True
    return False

def _yonergeleri_bul(kel, orta):
    """'Asagidaki ... 3. ve 4. sorulari cevaplayiniz' tipi yonergeleri bulur.
    Sutun bazlidir; 2-3 satira yayilan yonergeleri de yakalar."""
    parcalar = _yonerge_parcalari(kel, orta)
    parcalar.sort(key=lambda p: (-1 if p['kol'] is None else p['kol'], p['y0']))
    sonuc, i = [], 0
    while i < len(parcalar):
        bulundu = False
        for j in range(1, 4):
            grup = parcalar[i:i + j]
            if len(grup) < j:
                break
            if any(g['kol'] != grup[0]['kol'] for g in grup):
                break
            if any(grup[t]['y0'] - grup[t - 1]['y1'] > 0.9 * max(8.0, grup[t - 1]['y1'] - grup[t - 1]['y0'])
                   for t in range(1, j)):
                break
            metin = " ".join(g['metin'] for g in grup)
            mk = _tr_kucuk(metin)
            if _yonerge_soru_var(mk) and YONERGE_RE.search(mk):
                nums = _sayi_listesi(metin)
                if nums:
                    while (j < 4 and i + j < len(parcalar)
                           and not grup[-1]['metin'].rstrip().endswith(('.', ':', '?', '!'))):
                        sn = parcalar[i + j]
                        son = grup[-1]
                        if sn['kol'] != son['kol'] or sn['y0'] - son['y1'] > 0.9 * max(8.0, son['y1'] - son['y0']):
                            break
                        if len(sn['metin']) > 60:
                            break
                        grup = grup + [sn]
                        j += 1
                    sonuc.append({'nums': nums, 'kol': grup[0]['kol'],
                                  'x0': min(g['x0'] for g in grup), 'x1': max(g['x1'] for g in grup),
                                  'y0': min(g['y0'] for g in grup), 'y1': max(g['y1'] for g in grup)})
                    i += j
                    bulundu = True
                    break
        if not bulundu:
            i += 1
    return sonuc

def _span_boyutlari(page):
    sonuc = []
    try:
        for b in page.get_text("dict").get('blocks', []):
            for l in b.get('lines', []):
                for sp in l.get('spans', []):
                    if sp.get('text', '').strip():
                        x0, y0, x1, y1 = sp['bbox']
                        sonuc.append((x0, y0, x1, y1, float(sp['size'])))
    except Exception:
        pass
    return sonuc

def _boyut_bul(spans, k):
    cx, cy = (k[0] + k[2]) / 2, (k[1] + k[3]) / 2
    for x0, y0, x1, y1, sz in spans:
        if x0 - 0.5 <= cx <= x1 + 0.5 and y0 - 0.5 <= cy <= y1 + 0.5:
            return sz
    return None

def _numara_adaylari(kel, spans, ust, alt, sol_x, sag_x, orta):
    """'12.' biciminde her kelime icin: ardindan gelen metne gore punto orani ve bosluk."""
    import statistics
    boy_span = [sp[4] for sp in spans]
    boy_kel = [k[3] - k[1] for k in kel if k[3] > k[1]]
    govde_span = statistics.median(boy_span) if boy_span else None
    govde_kel = statistics.median(boy_kel) if boy_kel else None
    adaylar = []
    for k in kel:
        x0, y0, x1, y1, t = k
        m = NUMARA_RE.match(t)
        if not (m and ust < y0 < alt and sol_x <= x0 <= sag_x):
            continue
        h = y1 - y0
        yc = (y0 + y1) / 2
        yan = [q for q in kel if q is not k and q[0] >= x1 - 1 and abs((q[1] + q[3]) / 2 - yc) <= max(4.0, 0.6 * h)]
        sonraki = min(yan, key=lambda q: q[0]) if yan else None
        if sonraki is not None and sonraki[0] - x1 > 60:
            sonraki = None
        gap = (sonraki[0] - x1) if sonraki else None
        oran, olcu = None, None
        if sonraki is not None:
            s1, s2 = _boyut_bul(spans, k), _boyut_bul(spans, sonraki)
            if s1 and s2:
                oran, olcu = s1 / s2, 'punto'
            else:
                oran, olcu = h / max(1.0, sonraki[3] - sonraki[1]), 'yukseklik'
        esik = 1.02 if olcu == 'punto' else 1.05
        sz = _boyut_bul(spans, k)
        if sz and govde_span:
            govde_oran, govde_esik = sz / govde_span, 1.12
        elif govde_kel:
            govde_oran, govde_esik = h / govde_kel, 1.2
        else:
            govde_oran, govde_esik = None, 9
        buyuk = (oran is not None and oran >= esik) or (govde_oran is not None and govde_oran >= govde_esik)
        yalniz = sonraki is None
        tab = gap is not None and gap >= 4
        adaylar.append({'n': int(m.group(1)), 'x0': x0, 'y0': y0, 'kol': 0 if x0 < orta else 1,
                        'sonraki': sonraki is not None, 'gap': gap, 'oran': oran,
                        'buyuk': buyuk, 'yalniz': yalniz, 'tab': tab,
                        'makul': buyuk or yalniz or tab})
    return adaylar

def _numara_filtrele(adaylar, tani=None):
    """Soru numarasi KANITLARA gore (sert kural degil) degerlendirilir. Makul sayilmak icin biri yeter:
      - govde metninden ya da kendinden sonraki metinden buyuk puntolu,
      - ardindan sekme boslugu (>= 8 pt) var,
      - satirda tek basina (yanindaki kutu/resim metni uzakta).
    Makul olmayan ama makul bir numarayla (ayni sutunda) hizali olan numaralar kurtarilir."""
    def yaz(a, durum, neden=""):
        if tani is not None:
            o = "-" if a['oran'] is None else f"{a['oran']:.2f}"
            g = "-" if a['gap'] is None else f"{a['gap']:.1f}"
            tani.append(f"  aday {a['n']}. kol={a['kol']} x={a['x0']:.0f} y={a['y0']:.0f} "
                        f"punto_orani={o} bosluk={g} buyuk={a['buyuk']} yalniz={a['yalniz']} "
                        f"tab={a['tab']} -> {durum} {neden}")
    makul = [a for a in adaylar if a['makul']]
    if not makul:
        for a in adaylar:
            yaz(a, "kabul", "(hicbir aday ayirt edilemedi; sol kenar kuralina bakilir)")
        return list(adaylar)

    def hizali_mi(a, b):
        # Ayni basamak -> siki tolerans, farkli basamak -> genis tolerans
        a_bas = len(str(a['n'])); b_bas = len(str(b['n']))
        if a_bas == b_bas:
            return abs(b['x0'] - a['x0']) <= 3.0
        return abs(b['x0'] - a['x0']) <= 8.0

    kalan = []
    for a in adaylar:
        if a['makul']:
            kalan.append(a); yaz(a, "kabul"); continue
        if any(b['kol'] == a['kol'] and hizali_mi(a, b) for b in makul):
            kalan.append(a); yaz(a, "kabul", "(makul numarayla hizali)")
        else:
            yaz(a, "RED", "(metinle ayni boyutta, bitisik ve hizasiz: sekil/liste ici numara)")
    return kalan

def sayfa_gorselleri(page, vektor=False):
    """Sayfadaki gomulu resimlerin (ve istenirse vektor cizimlerin) dikdortgenleri."""
    sayfa = page.rect
    sayfa_alan = max(1.0, sayfa.width * sayfa.height)
    sonuc = []
    try:
        for info in page.get_image_info():
            r = fitz.Rect(info['bbox']) & sayfa
            if r.is_empty or r.width < 6 or r.height < 6 or r.get_area() > 0.7 * sayfa_alan:
                continue
            sonuc.append(r)
    except Exception:
        pass
    if vektor:
        try:
            for d in page.get_drawings():
                r = fitz.Rect(d['rect'])
                r = fitz.Rect(r.x0 - 0.6, r.y0 - 0.6, r.x1 + 0.6, r.y1 + 0.6) & sayfa
                if r.is_empty or r.get_area() > 0.5 * sayfa_alan:
                    continue
                if r.width < 3 and r.height < 3:
                    continue
                sonuc.append(r)
        except Exception:
            pass
    return sonuc

def _cevap_meta_tara(kel, soru_nolari=None, rects=None):
    if not kel:
        return {}
    H = max((k[3] for k in kel), default=800)

    # Satirlari daha hassas grupla (y merkezine gore)
    satirlar = _satirlara_bol(kel)
    sonuc = {}

    for s in satirlar:
        s = sorted(s, key=lambda k: k[0])
        metin = " ".join(t[4] for t in s)
        for m in re.finditer(r'(?<!\d)(\d{1,3})\s+([A-ZÇĞİÖŞÜ]{2,8})\s+([A-E])(?=\s+\d{3,}\b)', metin):
            n = int(m.group(1))
            cvp = m.group(3)
            if 1 <= n <= 500:
                sonuc[n] = cvp
                if rects is not None:
                    eslesen_kelimeler = [k for k in s if k[4] in [m.group(1), m.group(2), cvp] or re.search(r'\d{4,}', k[4])]
                    if eslesen_kelimeler:
                        rects.append(fitz.Rect(
                            min(k[0] for k in eslesen_kelimeler) - 2,
                            min(k[1] for k in eslesen_kelimeler) - 1,
                            max(k[2] for k in eslesen_kelimeler) + 2,
                            max(k[3] for k in eslesen_kelimeler) + 1
                        ))
    if sonuc:
        return sonuc

    # Ikinci yol: her satirin ilk kelimesi sayi ise ve satirda A-E harfleri varsa
    for s in satirlar:
        s = sorted(s, key=lambda k: k[0])
        if not s:
            continue
        ilk = s[0]
        if ilk[1] < 0.6 * H:
            continue
        if not re.fullmatch(r'\d{1,3}', ilk[4]):
            continue
        n0 = int(ilk[4])
        if not (1 <= n0 <= 500):
            continue
        harfler = [t[4] for t in s[1:] if re.fullmatch(r'[A-E]', t[4])]
        if not harfler:
            continue
        if rects is not None and ilk[1] >= 0.75 * H:
            ilgili = [k for k in s
                      if re.fullmatch(r'\d{1,3}', k[4])
                      or re.fullmatch(r'[A-E]', k[4])
                      or k[4] == ')']
            if ilgili:
                rects.append(fitz.Rect(
                    min(k[0] for k in ilgili) - 2,
                    min(k[1] for k in ilgili) - 1,
                    max(k[2] for k in ilgili) + 2,
                    max(k[3] for k in ilgili) + 1))
        if len(set(harfler)) == 1:
            sonuc.setdefault(n0, harfler[0])
        else:
            for i, h in enumerate(harfler):
                sonuc.setdefault(n0 + i, h)

    if sonuc:
        return sonuc

    if soru_nolari is None:
        soru_nolari = list(range(1, 101))

    adaylar = []
    for k in kel:
        x0, y0, x1, y1, t = k
        if not re.fullmatch(r'[A-E]', t):
            continue
        if y0 < 0.70 * H:
            continue
        opt = False
        for k2 in kel:
            if k2 is k:
                continue
            if k2[4] == ')' and abs(k2[1] - y0) < 6 and 0 < k2[0] - x1 < 8:
                opt = True
                break
        if opt:
            continue
        adaylar.append((y0, x0, t))

    if not adaylar:
        return sonuc

    satir_gruplari = {}
    for y, x, t in adaylar:
        satir_gruplari.setdefault(round(y / 5), []).append((x, t))

    harf_dizisi = []
    for y_key in sorted(satir_gruplari.keys()):
        grup = sorted(satir_gruplari[y_key], key=lambda p: p[0])
        onceki = None
        for x, t in grup:
            if t == onceki:
                continue
            harf_dizisi.append(t)
            onceki = t

    if not harf_dizisi:
        return sonuc

    if len(set(harf_dizisi)) == 1:
        if len(soru_nolari) == 1:
            sonuc[soru_nolari[0]] = harf_dizisi[0]
        return sonuc

    for i, h in enumerate(harf_dizisi[:len(soru_nolari)]):
        sonuc[soru_nolari[i]] = h

    return sonuc

def murekkep_haritasi(page, olcek=1.5):
    pix = page.get_pixmap(matrix=fitz.Matrix(olcek, olcek), alpha=False)
    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    r, g, b = img.split()
    en_koyu = ImageChops.darker(ImageChops.darker(r, g), b)
    return en_koyu.point(lambda v: 255 if v < 246 else 0), olcek

def _murekkep_kutusu(harita, rect):
    mask, olcek = harita
    x0, y0 = max(0, int(rect.x0 * olcek)), max(0, int(rect.y0 * olcek))
    x1, y1 = min(mask.width, int(rect.x1 * olcek) + 1), min(mask.height, int(rect.y1 * olcek) + 1)
    if x1 <= x0 or y1 <= y0:
        return None
    bb = mask.crop((x0, y0, x1, y1)).getbbox()
    if not bb:
        return None
    return fitz.Rect((x0 + bb[0]) / olcek, (y0 + bb[1]) / olcek, (x0 + bb[2]) / olcek, (y0 + bb[3]) / olcek)

def _kutu_hesapla(kel, harita, bolgeler, sayfa_rect, pad=5, yasak_x=(),
                  metin_pay_x=20, metin_pay_y=14, ekstra=()):
    def bolgede(cx, cy):
        return any(b.x0 <= cx <= b.x1 and b.y0 <= cy <= b.y1 for b in bolgeler)

    def yasakta(x0, x1):
        return any(x0 >= yx0 - 2 and x1 <= yx1 + 2 for yx0, yx1 in yasak_x)

    parcalar, harfler = [], set()
    metin_parcalari = []
    for x0, y0, x1, y1, t in kel:
        if not bolgede((x0 + x1) / 2, (y0 + y1) / 2):
            continue
        if yasakta(x0, x1):
            continue
        r = fitz.Rect(x0, y0, x1, y1)
        parcalar.append(r)
        metin_parcalari.append(r)
        m = SECENEK_RE.match(t)
        if m:
            harfler.add(m.group(1))

    if metin_parcalari:
        metin_bb = fitz.Rect(metin_parcalari[0])
        for r in metin_parcalari[1:]:
            metin_bb |= r
        arama = fitz.Rect(metin_bb.x0 - metin_pay_x, metin_bb.y0 - metin_pay_y,
                          metin_bb.x1 + metin_pay_x, metin_bb.y1 + metin_pay_y)
    else:
        arama = None

    for e in ekstra:
        alan = e.get_area()
        if alan <= 0:
            continue
        for b in bolgeler:
            ke = e & b
            if ke.is_empty or ke.width < 2 or ke.height < 2 or ke.get_area() < 0.5 * alan:
                continue
            if yasakta(ke.x0, ke.x1):
                continue
            parcalar.append(ke)
            if arama is not None:
                arama = fitz.Rect(min(arama.x0, ke.x0), min(arama.y0, ke.y0),
                                  max(arama.x1, ke.x1), max(arama.y1, ke.y1))

    for b in bolgeler:
        parca_bolgeler = [b]
        for yx0, yx1 in yasak_x:
            yeni = []
            for r in parca_bolgeler:
                if yx1 <= r.x0 or yx0 >= r.x1:
                    yeni.append(r)
                else:
                    if yx0 > r.x0:
                        yeni.append(fitz.Rect(r.x0, r.y0, yx0, r.y1))
                    if yx1 < r.x1:
                        yeni.append(fitz.Rect(yx1, r.y0, r.x1, r.y1))
            parca_bolgeler = yeni
        for r in parca_bolgeler:
            if r.width < 2 or r.height < 2:
                continue
            if arama is not None:
                r2 = r & arama
                if r2.is_empty or r2.width < 2 or r2.height < 2:
                    continue
                r = r2
            rr = _murekkep_kutusu(harita, r)
            if rr is not None:
                parcalar.append(rr)

    if not parcalar:
        return None, harfler
    birlesim = fitz.Rect(parcalar[0])
    for r in parcalar[1:]:
        birlesim |= r
    birlesim = fitz.Rect(birlesim.x0 - pad, birlesim.y0 - pad,
                         birlesim.x1 + pad, birlesim.y1 + pad)
    sinir = fitz.Rect(min(b.x0 for b in bolgeler), min(b.y0 for b in bolgeler),
                      max(b.x1 for b in bolgeler), max(b.y1 for b in bolgeler))
    birlesim &= sinir
    birlesim &= sayfa_rect
    return birlesim, harfler

def etkinlik_basliklari(kel, W, H):
    basliklar = []
    for x0, y0, x1, y1, t in kel:
        if not (re.fullmatch(r'\d{1,2}', t) and 0.33 * W <= x0 <= 0.40 * W and H * UST_ORAN < y0 < H * ALT_ORAN):
            continue
        etiket_kel = [k for k in kel if abs(k[1] - y0) < 4 and k[2] <= x0 and k[0] < x0 and k[0] >= 0.08 * W]
        etiket_kel.sort(key=lambda k: k[0])
        etiket = " ".join(k[4] for k in etiket_kel)
        if sum(c.isalpha() for c in etiket) < 3:
            continue
        basliklar.append({'n': int(t), 'y0': y0, 'etiket': f"{etiket} {t}"})
    basliklar.sort(key=lambda b: b['y0'])
    return basliklar

def sorulari_bul(page, zoom, secenekli_sart=True, etkinlik=True, kel=None, marj=None, tani=None):
    W, H = page.rect.width, page.rect.height
    if marj:
        sol_x = max(0.0, min(1.0, marj.get('sol', 0.0))) * W
        sag_x = max(0.0, min(1.0, marj.get('sag', 1.0))) * W
        ust_y = max(0.0, min(1.0, marj.get('ust', 0.0))) * H
        alt_y = max(0.0, min(1.0, marj.get('alt', 1.0))) * H
    else:
        sol_x, sag_x, ust_y, alt_y = 0.0, W, 0.0, H
    if sag_x <= sol_x + 5 or alt_y <= ust_y + 5:
        sol_x, sag_x, ust_y, alt_y = 0.0, W, 0.0, H

    orta = W / 2
    if kel is None:
        kel = sayfa_kelimeleri(page)

    ust = _baslik_alt_sinir(kel, W, H)
    ust = max(ust, ust_y)
    alt = H * ALT_ORAN
    cvp_y = _cevap_seridi_ust(kel, W, H)
    if cvp_y is not None:
        alt = min(alt, cvp_y - 4)
    alt = min(alt, alt_y)
    yasak_x = _dikey_metin_araliklari(kel)
    seviye_tol = H * 0.04
    harita = murekkep_haritasi(page)
    gorseller_soru = sayfa_gorselleri(page, vektor=True)
    gorseller_ortak = gorseller_soru
    spans = _span_boyutlari(page)

    def temel_sonuc(n, kol, rect, text, secenek_sayisi, etiket=None):
        d = {'q_num': n, 'kol': kol,
             'bbox_pdf': tuple(round(v, 1) for v in rect),
             'bbox_canvas': tuple(int(v * zoom) for v in rect),
             'text': text, 'secenek_sayisi': secenek_sayisi}
        if etiket:
            d['etiket'] = etiket
        return d

    def son_soru_alt_sinir(kol):
        if kol == 0:
            sutun_kel = [k for k in kel if (k[0] + k[2]) / 2 < orta and ust_y <= k[1] < H * 0.99]
        else:
            sutun_kel = [k for k in kel if (k[0] + k[2]) / 2 >= orta and ust_y <= k[1] < H * 0.99]
        if not sutun_kel:
            en_alt = H * 0.95
        else:
            satirlar = {}
            for k in sutun_kel:
                satirlar.setdefault(round(k[1] / 6), []).append(k)
            anahtarlar = sorted(satirlar.keys())
            while len(anahtarlar) >= 2:
                son, onceki = anahtarlar[-1], anahtarlar[-2]
                y0_son = min(k[1] for k in satirlar[son])
                y1_onceki = max(k[3] for k in satirlar[onceki])
                bosluk = y0_son - y1_onceki
                if bosluk > 22 and y0_son > H * 0.90:
                    anahtarlar.pop()
                else:
                    break
            en_alt = max((k[3] for a in anahtarlar for k in satirlar[a]), default=H * 0.95)
        sinir = min(H * 0.99, en_alt + 10)
        if cvp_y is not None:
            sinir = min(sinir, cvp_y - 4)
        sinir = min(sinir, alt_y)
        return sinir

    def numarali(sart):
        if tani is not None:
            tani.append(f"Sayfa {page.number + 1}: W={W:.0f} H={H:.0f} orta={orta:.0f} ust={ust:.0f} alt={alt:.0f}")
        adaylar = _numara_filtrele(_numara_adaylari(kel, spans, ust, alt, sol_x, sag_x, orta), tani)

        for kol in (0, 1):
            grup = [a for a in adaylar if a['kol'] == kol]
            if grup:
                sol = min(a['x0'] for a in grup)
                adaylar = [a for a in adaylar if a['kol'] != kol or a['x0'] <= sol + MARJ_TOL]

        yonergeler = []
        for yon in _yonergeleri_bul(kel, orta):
            yk = yon['kol']
            alt_adaylar = [a for a in adaylar if a['y0'] > yon['y1'] and (yk is None or a['kol'] == yk)]
            if yk == 0:
                arama_sol, arama_sag = sol_x, orta - 3
            elif yk == 1:
                arama_sol, arama_sag = orta + 3, sag_x
            else:
                arama_sol, arama_sag = sol_x, sag_x
            if alt_adaylar:
                ortak_alt = min(a['y0'] for a in alt_adaylar) - 2
            elif yk is None:
                ortak_alt = alt
            else:
                ortak_alt = son_soru_alt_sinir(yk)
            ortak_ust = yon['y1'] + 2
            if ortak_alt - ortak_ust < 6:
                continue
            arama_alani = fitz.Rect(arama_sol, ortak_ust, arama_sag, ortak_alt)
            ortak_rect, _ = _kutu_hesapla(kel, harita, [arama_alani], page.rect, yasak_x=yasak_x,
                                          pad=0, ekstra=gorseller_ortak)
            if tani is not None:
                tani.append(f"  yonerge nums={yon['nums']} kol={yk} y={yon['y0']:.0f}-{yon['y1']:.0f} -> ortak_rect="
                            + (str([round(v) for v in ortak_rect]) if ortak_rect else "yok"))
            if ortak_rect:
                yonergeler.append({'nums': yon['nums'], 'rect': ortak_rect, 'kol': yk,
                                   'y0': yon['y0'], 'y1': yon['y1']})

        sag_numaralar = [c['y0'] for c in adaylar if c['kol'] == 1]
        sorular = []
        for a in adaylar:
            ortak_rect = None
            ortak_yon = None
            for yon in yonergeler:
                if a['n'] in yon['nums']:
                    ortak_rect = yon['rect']
                    ortak_yon = yon
                    break

            ayni = [c['y0'] for c in adaylar if c['kol'] == a['kol'] and c['y0'] > a['y0'] + TOL]
            yon_ust = [y['y0'] for y in yonergeler
                       if (y['kol'] is None or y['kol'] == a['kol']) and y['y0'] > a['y0'] + TOL]
            ust_sinir = max(a['y0'] - 3, ust_y)
            if ortak_rect and a['y0'] >= ortak_rect.y1 - 1 and (ortak_yon['kol'] is None or ortak_yon['kol'] == a['kol']):
                ust_sinir = max(ust_sinir, ortak_rect.y1 + 2)

            yon_sinirda = bool(yon_ust) and min(yon_ust) <= min(ayni, default=1e9)
            if ayni or yon_ust:
                alt_sinir = min(ayni + yon_ust) - 4
            else:
                alt_sinir = son_soru_alt_sinir(a['kol'])
            alt_sinir = min(alt_sinir, alt_y)

            if a['kol'] == 0:
                bolgeler = [fitz.Rect(max(sol_x, a['x0'] - 8), ust_sinir, orta - 3, alt_sinir)]
                if not sag_numaralar or min(sag_numaralar) > a['y0'] + seviye_tol:
                    bitis = alt_sinir if not sag_numaralar else min(alt_sinir, min(sag_numaralar) - 4)
                    sag_ortaklar = [y for y in yonergeler if y['kol'] == 1 and y['y0'] < bitis and y['rect'].y1 > ust_sinir]
                    for y in sag_ortaklar:
                        bitis = min(bitis, y['y0'] - 4) if y['y0'] > ust_sinir else ust_sinir
                    if bitis > ust_sinir + 5:
                        bolgeler.append(fitz.Rect(orta + 3, ust_sinir, sag_x, bitis))
            else:
                bolgeler = [fitz.Rect(max(orta + 3, a['x0'] - 8), ust_sinir, sag_x, alt_sinir)]

            rect, harfler = _kutu_hesapla(kel, harita, bolgeler, page.rect, yasak_x=yasak_x,
                                          ekstra=gorseller_soru)
            if rect is None or (sart and len(harfler) < 2):
                if tani is not None:
                    tani.append(f"  soru {a['n']} ELENDI: " + ("kutu bos" if rect is None else f"sik sayisi {len(harfler)} < 2"))
                continue

            if ayni and not yon_sinirda and rect.y1 >= alt_sinir - 12:
                alt_sinir2 = min(alt, alt_y, min(ayni) + 16)
                if alt_sinir2 > alt_sinir + 0.5:
                    bolgeler2 = [fitz.Rect(b.x0, b.y0, b.x1, alt_sinir2) for b in bolgeler]
                    rect2, harfler2 = _kutu_hesapla(kel, harita, bolgeler2, page.rect, yasak_x=yasak_x,
                                                    ekstra=gorseller_soru)
                    if rect2 is not None and rect2.y1 > rect.y1 + 0.5:
                        rect, harfler = rect2, harfler2

            soru_dict = temel_sonuc(a['n'], a['kol'], rect, f"{a['n']}.", len(harfler))
            if ortak_rect:
                soru_dict['ortak_rect'] = tuple(round(v, 1) for v in ortak_rect)
            sorular.append(soru_dict)

        soru_nolari = sorted(set(s['q_num'] for s in sorular))
        cevaplar = _cevap_meta_tara(kel, soru_nolari)
        for s in sorular:
            if s['q_num'] in cevaplar:
                s['cevap'] = cevaplar[s['q_num']]

        sorular.sort(key=lambda s: (s['kol'], s['bbox_pdf'][1]))
        return sorular

    sorular = numarali(True)
    if sorular:
        return sorular

    if etkinlik:
        basliklar = etkinlik_basliklari(kel, W, H)
        if basliklar:
            ust_pay = 22
            bloklar = []
            for k, b in enumerate(basliklar):
                y_ust = max(b['y0'] - ust_pay, ust_y)
                y_alt = (basliklar[k + 1]['y0'] - ust_pay - 2) if k + 1 < len(basliklar) else alt
                y_alt = min(y_alt, alt_y)
                rect, _ = _kutu_hesapla(kel, harita, [fitz.Rect(sol_x + 5, y_ust, sag_x, y_alt)], page.rect, yasak_x=yasak_x)
                if rect is not None:
                    bloklar.append(temel_sonuc(b['n'], 0, rect, b['etiket'], 0, etiket=b['etiket']))
            return bloklar

    if not secenekli_sart:
        return numarali(False)
    return []

# ======================================================================
#  TEST GRUPLAMA + PDF URETIMI
# ======================================================================
ANAHTAR_RE = re.compile(r'(?i)^(deneme\w*|test|s\u0131nav\w*|\u00fcnite\w*)$')
BEKLENEN_RE = re.compile(r'(?i)\b(\d{1,3})\s+soru\s+var')

def sayfa_bilgisi(page, kel, baslik_marj=None):
    W, H = page.rect.width, page.rect.height
    if baslik_marj:
        bx0 = max(0.0, min(1.0, baslik_marj.get('sol', 0.0))) * W
        bx1 = max(0.0, min(1.0, baslik_marj.get('sag', 1.0))) * W
        by0 = max(0.0, min(1.0, baslik_marj.get('ust', 0.0))) * H
        by1 = max(0.0, min(1.0, baslik_marj.get('alt', 1.0))) * H
    else:
        bx0, bx1, by0, by1 = 0.0, W, 0.0, H

    def baslikta(k):
        cx = (k[0] + k[2]) / 2
        cy = (k[1] + k[3]) / 2
        return bx0 <= cx <= bx1 and by0 <= cy <= by1

    baslik_kelimeleri = [k for k in kel if baslikta(k)]
    satir_sirali = sorted(baslik_kelimeleri, key=lambda k: (round(k[1] / 6), k[0]))
    duz_metin = " ".join(k[4] for k in satir_sirali)
    m = BEKLENEN_RE.search(duz_metin)
    beklenen = int(m.group(1)) if m else None

    ust_band = [k for k in baslik_kelimeleri if k[1] < 0.16 * H]

    def soru_sayisi_rakami(k):
        return any(w[4].lower().startswith('soru') and abs(w[1] - k[1]) < 6 and 0 < w[0] - k[2] < 40 for w in kel)

    rakamlar = [k for k in ust_band if re.fullmatch(r'\d{1,3}', k[4]) and k[1] < 0.14 * H and not soru_sayisi_rakami(k)]
    test_no, en_iyi, kw_secili = None, None, None
    for kw in (k for k in ust_band if ANAHTAR_RE.match(k[4])):
        kcy = (kw[1] + kw[3]) / 2
        for d in rakamlar:
            dcy = (d[1] + d[3]) / 2
            yatay = max(0, d[0] - kw[2], kw[0] - d[2])
            if abs(dcy - kcy) < 0.07 * H and yatay < 0.2 * W:
                skor = yatay + abs(dcy - kcy)
                if en_iyi is None or skor < en_iyi:
                    en_iyi, test_no, kw_secili = skor, int(d[4]), kw

    baslik_kel = sorted((k for k in baslik_kelimeleri
                         if 0.025 * H < k[1] < 0.115 * H and 0.08 * W < k[0] < 0.92 * W
                         and not re.fullmatch(r'\d{1,3}', k[4])
                         and not NUMARA_RE.match(k[4])
                         and k[4].upper() != 'TEST'), key=lambda k: k[0])
    baslik = " ".join(k[4] for k in baslik_kel)
    if kw_secili is not None and re.match(r'(?i)^(deneme|s\u0131nav)', kw_secili[4]):
        kcy = (kw_secili[1] + kw_secili[3]) / 2
        satir = sorted((k for k in ust_band
                        if abs((k[1] + k[3]) / 2 - kcy) < 9 and k[2] <= kw_secili[2] + 1
                        and k[0] >= kw_secili[0] - 0.45 * W and not re.fullmatch(r'\d{1,3}', k[4])), key=lambda k: k[0])
        if satir:
            baslik = " ".join(k[4] for k in satir) + f" {test_no}"

    alt = [k for k in kel if k[1] > 0.935 * H]
    ders = " ".join(k[4] for k in sorted(alt, key=lambda k: k[0]) if re.fullmatch(r'[^\W\d_]+', k[4]))
    kitap = next((int(k[4]) for k in sorted(alt, key=lambda k: k[0]) if re.fullmatch(r'\d{1,3}', k[4])), None)

    return {'test_no': test_no, 'beklenen': beklenen, 'baslik': baslik, 'ders': ders, 'kitap_sayfa': kitap, 'w': W}

def aralik_yaz(sayilar):
    sayilar = sorted(set(sayilar))
    parcalar, i = [], 0
    while i < len(sayilar):
        j = i
        while j + 1 < len(sayilar) and sayilar[j + 1] == sayilar[j] + 1:
            j += 1
        parcalar.append(str(sayilar[i]) if i == j else f"{sayilar[i]}\u2013{sayilar[j]}")
        i = j + 1
    return ", ".join(parcalar)

def testleri_grupla(tum_kutular, bilgi_al, zorla=None):
    zorla = zorla or {}
    testler, onceki = [], None
    for p in sorted(k for k, v in tum_kutular.items() if v):
        b = bilgi_al(p)
        W = b.get('w', 570)
        sirali = sorted(tum_kutular[p], key=lambda k: (0 if k['bbox_pdf'][0] < 0.45 * W else 1, k['bbox_pdf'][1]))
        soru_nolari = [k['q_num'] for k in sirali if not k.get('etiket')]

        if not testler:
            yeni = True
        elif p in zorla:
            yeni = bool(zorla[p])
        else:
            yeni = bool(soru_nolari) and soru_nolari[0] == 1

        if yeni:
            t = {'sayfalar': [], 'kitap': [], 'ders': b.get('ders', ''), 'baslik': b.get('baslik', ''),
                 'beklenen': None, 'sorular': [], 'baslangic_sayfa': p}
            testler.append(t)
            onceki = (b.get('ders', ''), b.get('test_no'))
        else:
            t = testler[-1]
        if not t['baslik'] and b.get('baslik'):
            t['baslik'] = b['baslik']
        if t['beklenen'] is None and b.get('beklenen') is not None:
            t['beklenen'] = b['beklenen']
        t['sayfalar'].append(p)
        if b.get('kitap_sayfa') is not None:
            t['kitap'].append(b['kitap_sayfa'])
        for k in sirali:
            t['sorular'].append((p, k))
    tum_kitap = [x for t in testler for x in t['kitap']]
    kitap_guvenilir = len(tum_kitap) == len(set(tum_kitap))
    for n, t in enumerate(testler, 1):
        t['no'] = n
        t['ad'] = f"Test-{n}"
        pdf_yazi = aralik_yaz([p + 1 for p in t['sayfalar']])
        t['sayfa_yazi'] = (f"s. {aralik_yaz(t['kitap'])} (PDF {pdf_yazi})" if t['kitap'] and kitap_guvenilir else f"PDF s. {pdf_yazi}")
        t['konu'] = " - ".join(x for x in (t['ders'], t['baslik']) if x)
        adet = len(t['sorular'])
        t['uyari'] = t['beklenen'] is not None and t['beklenen'] != adet
        t['soru_yazi'] = f"{adet}/{t['beklenen']} !" if t['uyari'] else (
            f"{adet}/{t['beklenen']}" if t['beklenen'] is not None else str(adet))
        soru_kel = [k for _, k in t['sorular'] if not k.get('etiket')]
        t['cevap_anahtari'] = "".join(k.get('cevap', '?') for k in soru_kel)
        t['cevap_bilinen'] = sum(1 for k in soru_kel if k.get('cevap'))
    return testler

def cevap_satiri_dikdortgenleri(kel):
    """Cevabin okundugu satirlar (ornek: '13 FEN B 3902...' satiri, sayfa altindaki cevap seridi)."""
    if not kel:
        return []
    H = max(k[3] for k in kel)
    W = max(k[2] for k in kel)
    rects = []
    _cevap_meta_tara(kel, None, rects)
    _cevap_seridi_ust(kel, W, H, rects)
    sonuc = []
    for r in rects:
        # Dikeyde ±1 pt ile genislet (yanlislikla komsu satira tasmasin)
        r = fitz.Rect(r.x0 - 2, r.y0 - 1, r.x1 + 2, r.y1 + 1)
        if not any(abs(r.x0 - q.x0) < 1 and abs(r.y0 - q.y0) < 1 and abs(r.x1 - q.x1) < 1 and abs(r.y1 - q.y1) < 1 for q in sonuc):
            sonuc.append(r)
    return sonuc

def cevap_satirlarini_ele(clip, cevap_rects, kel):
    """
    Cevap satirlarini sadece kendi koordinatlari icinde beyazlatir.
    Kesisim alaninda sik isareti (A), B) ...) varsa o kutu beyazlatilmaz (guvenlik).
    """
    beyazlar = []
    for r in cevap_rects:
        kesisim = r & clip
        if kesisim.is_empty or kesisim.height <= 2:
            continue
        # GUVENLIK: bu alanda sik isareti varsa silme
        sik_var = any(
            SECENEK_RE.match(k[4])
            and kesisim.x0 - 2 <= k[0] and k[2] <= kesisim.x1 + 2
            and kesisim.y0 - 2 <= k[1] and k[3] <= kesisim.y1 + 2
            for k in kel
        )
        if sik_var:
            continue
        beyazlar.append(kesisim)
    return clip, beyazlar

def _beyazla(yeni, beyazlar, ox, oy):
    for r in beyazlar:
        if r.is_empty:
            continue
        yeni.draw_rect(fitz.Rect(r.x0 + ox, r.y0 + oy, r.x1 + ox, r.y1 + oy), color=None, fill=(1, 1, 1))

def numarayi_gizle(clip, kel):
    adaylar = [w for w in kel if NUMARA_RE.match(w[4])
               and clip.x0 - 3 <= w[0] <= clip.x0 + 70 and clip.y0 - 3 <= w[1] <= clip.y0 + 35]
    if not adaylar:
        return clip
    w = min(adaylar, key=lambda w: (round(w[1]), w[0]))
    return fitz.Rect(max(clip.x0, w[2] + 2), clip.y0, clip.x1, clip.y1)

def test_pdf_yaz(pdf_doc, kilit, sorular, yol, raster=False, kelime_onbellek=None, kenar=8, cevap_gizle=True, cevap_satiri_sil=True):
    kelime_onbellek = {} if kelime_onbellek is None else kelime_onbellek
    cikis = fitz.open()
    for soru_verisi in sorular:
        pno, bbox, blok = soru_verisi[:3]
        ortak_rect = soru_verisi[3] if len(soru_verisi) > 3 else None

        with kilit:
            page = pdf_doc.load_page(pno)
            clip_soru = fitz.Rect(bbox) & page.rect

            if ortak_rect:
                clip_ortak = fitz.Rect(ortak_rect) & page.rect

                if pno not in kelime_onbellek:
                    kelime_onbellek[pno] = sayfa_kelimeleri(page)
                beyaz_soru, beyaz_ortak = [], []
                if not blok:
                    clip_soru = numarayi_gizle(clip_soru, kelime_onbellek[pno])
                if cevap_satiri_sil and not blok:
                    if ('cr', pno) not in kelime_onbellek:
                        kelime_onbellek[('cr', pno)] = cevap_satiri_dikdortgenleri(kelime_onbellek[pno])
                    clip_soru, beyaz_soru = cevap_satirlarini_ele(clip_soru, kelime_onbellek[('cr', pno)], kelime_onbellek[pno])
                    clip_ortak, beyaz_ortak = cevap_satirlarini_ele(clip_ortak, kelime_onbellek[('cr', pno)], kelime_onbellek[pno])
                if clip_soru.is_empty or clip_soru.width < 5 or clip_soru.height < 5:
                    continue

                bosluk = 15
                yeni_genislik = max(clip_ortak.width, clip_soru.width) + 2 * kenar
                yeni_yukseklik = clip_ortak.height + clip_soru.height + bosluk + 2 * kenar

                yeni = cikis.new_page(width=yeni_genislik, height=yeni_yukseklik)

                hedef_ortak = fitz.Rect(kenar, kenar, kenar + clip_ortak.width, kenar + clip_ortak.height)
                if raster:
                    pix = page.get_pixmap(clip=clip_ortak, dpi=200, alpha=False)
                    yeni.insert_image(hedef_ortak, pixmap=pix)
                else:
                    yeni.show_pdf_page(hedef_ortak, pdf_doc, pno, clip=clip_ortak)

                hedef_soru = fitz.Rect(kenar, kenar + clip_ortak.height + bosluk,
                                       kenar + clip_soru.width, kenar + clip_ortak.height + bosluk + clip_soru.height)
                if raster:
                    pix = page.get_pixmap(clip=clip_soru, dpi=200, alpha=False)
                    yeni.insert_image(hedef_soru, pixmap=pix)
                else:
                    yeni.show_pdf_page(hedef_soru, pdf_doc, pno, clip=clip_soru)

                _beyazla(yeni, beyaz_ortak, kenar - clip_ortak.x0, kenar - clip_ortak.y0)
                _beyazla(yeni, beyaz_soru, kenar - clip_soru.x0, kenar + clip_ortak.height + bosluk - clip_soru.y0)

                if pno not in kelime_onbellek:
                    kelime_onbellek[pno] = sayfa_kelimeleri(page)

                if cevap_gizle:
                    isaretliler = isaretli_secenekler(page, clip_soru, kelime_onbellek[pno])
                    for w in isaretliler:
                        ox = kenar - clip_soru.x0
                        oy = kenar + clip_ortak.height + bosluk - clip_soru.y0

                        yeni.draw_rect(fitz.Rect(w[0] + ox - 4, w[1] + oy - 4, w[2] + ox + 4, w[3] + oy + 4),
                                       color=None, fill=(1, 1, 1))
                        boy = (w[2] - w[0]) / max(0.1, fitz.get_text_length(w[4], fontname="helv", fontsize=1))
                        boy = min(14.0, max(6.0, boy))
                        komsu = [k for k in kelime_onbellek[pno] if k is not w and 0 < k[0] - w[2] < 40
                                 and abs((k[1] + k[3]) / 2 - (w[1] + w[3]) / 2) < 8 and not SECENEK_TAM_RE.match(k[4])]
                        taban = (min(komsu, key=lambda k: k[0] - w[2])[3] if komsu else w[3]) - 0.22 * boy
                        yeni.insert_text(fitz.Point(w[0] + ox, taban + oy), w[4], fontname="helv",
                                         fontsize=boy, color=(0, 0, 0))

            else:
                clip = clip_soru
                if not blok:
                    if pno not in kelime_onbellek:
                        kelime_onbellek[pno] = sayfa_kelimeleri(page)
                    clip = numarayi_gizle(clip, kelime_onbellek[pno])

                if pno not in kelime_onbellek:
                    kelime_onbellek[pno] = sayfa_kelimeleri(page)
                beyazlar = []
                if cevap_satiri_sil and not blok:
                    if ('cr', pno) not in kelime_onbellek:
                        kelime_onbellek[('cr', pno)] = cevap_satiri_dikdortgenleri(kelime_onbellek[pno])
                    clip, beyazlar = cevap_satirlarini_ele(clip, kelime_onbellek[('cr', pno)], kelime_onbellek[pno])
                if clip.is_empty or clip.width < 5 or clip.height < 5:
                    continue
                isaretliler = isaretli_secenekler(page, clip, kelime_onbellek[pno]) if cevap_gizle else []
                yeni = cikis.new_page(width=clip.width + 2 * kenar, height=clip.height + 2 * kenar)
                hedef = fitz.Rect(kenar, kenar, kenar + clip.width, kenar + clip.height)
                if raster:
                    pix = page.get_pixmap(clip=clip, dpi=200, alpha=False)
                    yeni.insert_image(hedef, pixmap=pix)
                else:
                    yeni.show_pdf_page(hedef, pdf_doc, pno, clip=clip)
                _beyazla(yeni, beyazlar, kenar - clip.x0, kenar - clip.y0)

                for w in isaretliler:
                    ox, oy = kenar - clip.x0, kenar - clip.y0
                    yeni.draw_rect(fitz.Rect(w[0] + ox - 4, w[1] + oy - 4, w[2] + ox + 4, w[3] + oy + 4),
                                   color=None, fill=(1, 1, 1))
                    boy = (w[2] - w[0]) / max(0.1, fitz.get_text_length(w[4], fontname="helv", fontsize=1))
                    boy = min(14.0, max(6.0, boy))
                    komsu = [k for k in kelime_onbellek[pno] if k is not w and 0 < k[0] - w[2] < 40
                             and abs((k[1] + k[3]) / 2 - (w[1] + w[3]) / 2) < 8 and not SECENEK_TAM_RE.match(k[4])]
                    taban = (min(komsu, key=lambda k: k[0] - w[2])[3] if komsu else w[3]) - 0.22 * boy
                    yeni.insert_text(fitz.Point(w[0] + ox, taban + oy), w[4], fontname="helv",
                                     fontsize=boy, color=(0, 0, 0))
    sayfa_sayisi = len(cikis)
    if sayfa_sayisi:
        cikis.save(yol, garbage=3, deflate=True)
    cikis.close()
    return sayfa_sayisi

SECENEK_TAM_RE = re.compile(r'^[A-E]\)$')

def isaretli_secenekler(page, clip, kel, dpi=144):
    adaylar = [w for w in kel if SECENEK_TAM_RE.match(w[4])
               and clip.x0 - 2 <= (w[0] + w[2]) / 2 <= clip.x1 + 2 and clip.y0 - 2 <= (w[1] + w[3]) / 2 <= clip.y1 + 2]
    if not adaylar:
        return []
    z = dpi / 72.0
    pix = page.get_pixmap(matrix=fitz.Matrix(z, z), clip=clip, alpha=False)
    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    r, g, b = img.split()
    kirmizi = ImageChops.darker(ImageChops.darker(r.point(lambda v: 255 if v > 180 else 0),
                                                  g.point(lambda v: 255 if v < 110 else 0)),
                                b.point(lambda v: 255 if v < 110 else 0))
    bulunan = []
    for w in adaylar:
        x0, y0 = int((w[0] - 3 - clip.x0) * z), int((w[1] - 3 - clip.y0) * z)
        x1, y1 = int((w[2] + 3 - clip.x0) * z) + 1, int((w[3] + 3 - clip.y0) * z) + 1
        x0, y0, x1, y1 = max(0, x0), max(0, y0), min(kirmizi.width, x1), min(kirmizi.height, y1)
        if x1 > x0 and y1 > y0 and kirmizi.crop((x0, y0, x1, y1)).histogram()[255] >= 6:
            bulunan.append(w)
    return bulunan

def dosya_adi_temizle(ad):
    return re.sub(r'\s+', ' ', re.sub(r'[\\/:*?"<>|]+', ' ', ad)).strip()[:120]

# ======================================================================
#  COZUM MODU PENCERESI
# ======================================================================
class CozumPenceresi(tk.Toplevel):
    RENKLER = ["#000000", "#e53935", "#1e88e5", "#43a047",
               "#fb8c00", "#8e24aa", "#fdd835", "#00897b"]

    def __init__(self, master, pdf_doc, pdf_kilit, sayfa_no, bbox_pdf, baslik=""):
        super().__init__(master)
        self.pdf_doc = pdf_doc
        self.pdf_kilit = pdf_kilit
        self.sayfa_no = sayfa_no
        self.bbox_pdf = list(bbox_pdf)
        self.title(baslik or "Z-Kitap Modu")
        self.configure(bg="#eceff1")
        self.mod = "kalem"
        self.renk = self.RENKLER[0]
        self.kalinlik = 4
        self.cizim_aktif = False
        self.son_nokta = None
        self.stroke_gruplari = []
        self.gecici_items = []
        self.attributes("-fullscreen", True)
        self.update_idletasks()
        self._soruyu_hazirla()
        self._araclari_olustur()
        self.bind("<Escape>", lambda e: self.destroy())
        self.bind("<F11>", lambda e: self.attributes("-fullscreen", not self.attributes("-fullscreen")))

    def _soruyu_hazirla(self):
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.margin = 20
        with self.pdf_kilit:
            page = self.pdf_doc.load_page(self.sayfa_no)
            pad = 5
            clip = fitz.Rect(self.bbox_pdf[0] - pad, self.bbox_pdf[1] - pad,
                             self.bbox_pdf[2] + pad, self.bbox_pdf[3] + pad) & page.rect
            pix = page.get_pixmap(clip=clip, dpi=170, alpha=False)
        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        max_w = int(sw * 0.58)
        max_h = int(sh * 0.86)
        scale = min(max_w / img.width, max_h / img.height, 1.0)
        if scale < 1.0:
            img = img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)
        self.img_tk = ImageTk.PhotoImage(img)
        self.img_w, self.img_h = img.width, img.height
        self.canvas = tk.Canvas(self, bg="#ffffff", highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.canvas.create_image(self.margin, self.margin, anchor=tk.NW, image=self.img_tk, tags="arka")
        x_sep = self.margin + self.img_w
        self.canvas.create_line(x_sep, self.margin, x_sep, self.margin + self.img_h, fill="#b0bec5", dash=(4, 3), tags="arka")
        self.canvas.bind("<Button-1>", self._basla)
        self.canvas.bind("<B1-Motion>", self._surukle)
        self.canvas.bind("<ButtonRelease-1>", self._bitir)

    def _araclari_olustur(self):
        self.arac = tk.Frame(self, bg="#263238", bd=1, relief=tk.RAISED, highlightthickness=1, highlightbackground="#455a64")
        self.arac.place(x=24, y=24)
        tut = tk.Label(self.arac, text=" \u28ff ", bg="#37474f", fg="white", font=("Arial", 13, "bold"), cursor="fleur")
        tut.pack(side=tk.LEFT, fill=tk.Y)
        tut.bind("<Button-1>", self._tut_basla)
        tut.bind("<B1-Motion>", self._tut_surukle)
        self.icerik = tk.Frame(self.arac, bg="#263238")
        self.icerik.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(4, 2), pady=4)
        r1 = tk.Frame(self.icerik, bg="#263238"); r1.pack(side=tk.TOP, fill=tk.X)
        self.kalem_btn = self._arac_btn(r1, " \u270e Kalem ", lambda: self._mod_sec("kalem"), True)
        self.kalem_btn.pack(side=tk.LEFT, padx=1)
        self.silgi_btn = self._arac_btn(r1, " \u232b Silgi ", lambda: self._mod_sec("silgi"), False)
        self.silgi_btn.pack(side=tk.LEFT, padx=1)
        self.temizle_btn = self._arac_btn(r1, " \u27f2 Tumunu Sil ", self._tumunu_sil, False)
        self.temizle_btn.pack(side=tk.LEFT, padx=1)
        r2 = tk.Frame(self.icerik, bg="#263238"); r2.pack(side=tk.TOP, fill=tk.X, pady=(5, 0))
        tk.Label(r2, text="Renk:", bg="#263238", fg="white", font=("Arial", 9)).pack(side=tk.LEFT, padx=(0, 4))
        self.renk_btnlari = {}
        for c in self.RENKLER:
            b = tk.Button(r2, bg=c, width=2, height=1, bd=1, relief=tk.RAISED, activebackground=c, command=lambda c=c: self._renk_sec(c))
            b.pack(side=tk.LEFT, padx=1)
            self.renk_btnlari[c] = b
        self._renk_sec(self.renk)
        r3 = tk.Frame(self.icerik, bg="#263238"); r3.pack(side=tk.TOP, fill=tk.X, pady=(5, 0))
        tk.Label(r3, text="Kalinlik:", bg="#263238", fg="white", font=("Arial", 9)).pack(side=tk.LEFT, padx=(0, 4))
        self.kalinlik_var = tk.IntVar(value=self.kalinlik)
        for w in (2, 4, 6, 10, 16):
            tk.Radiobutton(r3, text=str(w), variable=self.kalinlik_var, value=w, command=self._kalinlik_ayarla,
                           bg="#263238", fg="white", selectcolor="#37474f", activebackground="#263238", activeforeground="white", font=("Arial", 9)).pack(side=tk.LEFT, padx=1)
        self.daralt_btn = tk.Button(self.arac, text=" \u25be ", command=self._daralt_degistir, bg="#37474f", fg="white", bd=0, relief=tk.FLAT, activebackground="#546e7a", activeforeground="white", font=("Arial", 11, "bold"))
        self.daralt_btn.pack(side=tk.RIGHT, fill=tk.Y)
        self._daraltildi = False

    def _arac_btn(self, parent, text, cmd, aktif):
        return tk.Button(parent, text=text, command=cmd, bd=0, bg="#455a64" if aktif else "#37474f", fg="white", activebackground="#546e7a", activeforeground="white", font=("Arial", 10, "bold"), padx=6, pady=3)

    def _mod_sec(self, mod):
        self.mod = mod
        self.kalem_btn.config(bg="#455a64" if mod == "kalem" else "#37474f")
        self.silgi_btn.config(bg="#455a64" if mod == "silgi" else "#37474f")
        try:
            self.canvas.config(cursor="pencil" if mod == "kalem" else "dotbox")
        except tk.TclError:
            self.canvas.config(cursor="crosshair")

    def _renk_sec(self, c):
        self.renk = c
        for cc, b in self.renk_btnlari.items():
            b.config(relief=tk.SUNKEN if cc == c else tk.RAISED, bd=3 if cc == c else 1)

    def _kalinlik_ayarla(self): self.kalinlik = int(self.kalinlik_var.get())
    def _tumunu_sil(self):
        for grp in self.stroke_gruplari:
            for it in grp: self.canvas.delete(it)
        self.stroke_gruplari = []

    def _daralt_degistir(self):
        if self._daraltildi:
            self.icerik.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(4, 2), pady=4, before=self.daralt_btn)
            self.daralt_btn.config(text=" \u25be ")
        else:
            self.icerik.pack_forget()
            self.daralt_btn.config(text=" \u25b8 ")
        self._daraltildi = not self._daraltildi

    def _tut_basla(self, e):
        self._tut_dx = e.x_root - self.arac.winfo_rootx()
        self._tut_dy = e.y_root - self.arac.winfo_rooty()

    def _tut_surukle(self, e):
        nx = e.x_root - self.winfo_rootx() - self._tut_dx
        ny = e.y_root - self.winfo_rooty() - self._tut_dy
        nw, nh = self.winfo_width(), self.winfo_height()
        aw, ah = self.arac.winfo_width(), self.arac.winfo_height()
        nx = max(0, min(nw - aw, nx))
        ny = max(0, min(nh - ah, ny))
        self.arac.place(x=nx, y=ny)

    def _basla(self, e):
        x, y = self.canvas.canvasx(e.x), self.canvas.canvasy(e.y)
        if self.mod == "silgi":
            self._silgi_uygula(x, y); return
        self.cizim_aktif = True
        self.son_nokta = (x, y)
        r = self.kalinlik / 2
        it = self.canvas.create_oval(x - r, y - r, x + r, y + r, fill=self.renk, outline=self.renk, tags="cizim")
        self.gecici_items = [it]

    def _surukle(self, e):
        if not self.cizim_aktif: return
        x, y = self.canvas.canvasx(e.x), self.canvas.canvasy(e.y)
        if self.son_nokta:
            it = self.canvas.create_line(self.son_nokta[0], self.son_nokta[1], x, y, fill=self.renk, width=self.kalinlik, capstyle=tk.ROUND, joinstyle=tk.ROUND, tags="cizim")
            self.gecici_items.append(it)
        self.son_nokta = (x, y)

    def _bitir(self, e):
        self.cizim_aktif = False
        self.son_nokta = None
        if self.gecici_items:
            self.stroke_gruplari.append(self.gecici_items)
            self.gecici_items = []

    def _silgi_uygula(self, x, y):
        r = max(14, self.kalinlik * 4)
        hedef = None
        for grp in self.stroke_gruplari:
            for it in grp:
                bb = self.canvas.bbox(it)
                if bb and bb[0] - r <= x <= bb[2] + r and bb[1] - r <= y <= bb[3] + r:
                    hedef = grp; break
            if hedef: break
        if hedef:
            for it in hedef: self.canvas.delete(it)
            self.stroke_gruplari.remove(hedef)

# ======================================================================
#  ORTAK ALAN DUZENLEME PENCERESI
# ======================================================================
class OrtakAlanDuzenle(tk.Toplevel):
    def __init__(self, master, nums_str, rect, callback):
        super().__init__(master)
        self.title("Ortak Alan Düzenle")
        self.geometry("350x200")
        self.callback = callback
        self.rect = rect

        tk.Label(self, text="Bu ortak alanın ait olduğu soru numaraları\n(Örn: 19, 20)", justify=tk.LEFT).pack(pady=5)
        self.nums_entry = tk.Entry(self, width=30)
        self.nums_entry.insert(0, nums_str)
        self.nums_entry.pack(pady=5)

        tk.Label(self, text="Konum ve Boyut (X, Y, Genişlik, Yükseklik)").pack(pady=5)
        frame = tk.Frame(self)
        frame.pack()

        self.entries = []
        for i, val in enumerate([rect[0], rect[1], rect[2]-rect[0], rect[3]-rect[1]]):
            e = tk.Entry(frame, width=8)
            e.insert(0, str(round(val, 1)))
            e.pack(side=tk.LEFT, padx=2)
            self.entries.append(e)

        tk.Button(self, text="Kaydet", command=self.kaydet, bg="#4CAF50", fg="white").pack(pady=10)

    def kaydet(self):
        try:
            nums = [int(n.strip()) for n in self.nums_entry.get().split(",") if n.strip().isdigit()]
            x = float(self.entries[0].get())
            y = float(self.entries[1].get())
            w = float(self.entries[2].get())
            h = float(self.entries[3].get())
            new_rect = [x, y, x+w, y+h]
            self.callback(nums, new_rect)
            self.destroy()
        except ValueError:
            messagebox.showerror("Hata", "Lütfen geçerli sayılar girin.")

# ======================================================================
#  AKILLI TAHTA
# ======================================================================
TAHTA_ZOOM = 1.6

def _tahta_render(doc, pno, zoom=TAHTA_ZOOM):
    pix = doc.load_page(pno).get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)

def tahta_fark_maskesi(img_a, img_b, esik):
    import numpy as np
    a = np.asarray(img_a, dtype=np.int16)
    b = np.asarray(img_b, dtype=np.int16)
    fark = np.abs(a - b).sum(axis=2)
    a_beyaz = (a >= 235).all(axis=2)
    b_beyaz = (b >= 235).all(axis=2)
    return (fark >= esik) & ~(a_beyaz & b_beyaz)

def tahta_genislet(mask, R):
    import numpy as np
    k = 2 * R + 1
    m = mask.astype(np.int32)
    p = np.pad(m, ((0, 0), (R, R)))
    c = np.concatenate([np.zeros((p.shape[0], 1), np.int32), p.cumsum(axis=1)], axis=1)
    m = (c[:, k:] - c[:, :-k]) > 0
    p = np.pad(m.astype(np.int32), ((R, R), (0, 0)))
    c = np.concatenate([np.zeros((1, p.shape[1]), np.int32), p.cumsum(axis=0)], axis=0)
    return (c[k:, :] - c[:-k, :]) > 0

def tahta_bilesenler(mask, min_alan=250, min_w=22, min_h=10):
    import numpy as np
    sonuc = []
    try:
        from scipy import ndimage
    except ImportError:
        ndimage = None
    if ndimage is not None:
        etiket, n = ndimage.label(mask)
        if n == 0:
            return []
        alanlar = np.bincount(etiket.ravel(), minlength=n + 1)
        for i, sl in enumerate(ndimage.find_objects(etiket), 1):
            if sl is None:
                continue
            y0, y1, x0, x1 = sl[0].start, sl[0].stop, sl[1].start, sl[1].stop
            alan, rw, rh = int(alanlar[i]), x1 - x0, y1 - y0
            if alan < min_alan or rw < min_w or rh < min_h:
                continue
            sonuc.append({'x': x0, 'y': y0, 'w': rw, 'h': rh, 'area': alan})
        return sonuc
    f = 4
    h, w = mask.shape
    H4, W4 = -(-h // f), -(-w // f)
    pad = np.zeros((H4 * f, W4 * f), bool)
    pad[:h, :w] = mask
    kucuk = pad.reshape(H4, f, W4, f).any(axis=(1, 3))
    ziyaret = np.zeros_like(kucuk)
    ys, xs = np.nonzero(kucuk)
    for y, x in zip(ys.tolist(), xs.tolist()):
        if ziyaret[y, x]:
            continue
        yigin = [(y, x)]
        ziyaret[y, x] = True
        mnx = mxx = x
        mny = mxy = y
        while yigin:
            cy, cx = yigin.pop()
            mnx, mxx = min(mnx, cx), max(mxx, cx)
            mny, mxy = min(mny, cy), max(mxy, cy)
            for ny, nx in ((cy - 1, cx), (cy + 1, cx), (cy, cx - 1), (cy, cx + 1)):
                if 0 <= ny < H4 and 0 <= nx < W4 and kucuk[ny, nx] and not ziyaret[ny, nx]:
                    ziyaret[ny, nx] = True
                    yigin.append((ny, nx))
        blok = pad[mny * f:(mxy + 1) * f, mnx * f:(mxx + 1) * f]
        satir = np.nonzero(blok.any(axis=1))[0]
        sutun = np.nonzero(blok.any(axis=0))[0]
        if len(satir) == 0:
            continue
        x0, x1 = mnx * f + int(sutun[0]), mnx * f + int(sutun[-1]) + 1
        y0, y1 = mny * f + int(satir[0]), mny * f + int(satir[-1]) + 1
        alan, rw, rh = int(blok.sum()), x1 - x0, y1 - y0
        if alan < min_alan or rw < min_w or rh < min_h:
            continue
        sonuc.append({'x': x0, 'y': y0, 'w': rw, 'h': rh, 'area': alan})
    return sonuc

def tahta_grupla(bolgeler, max_dikey, max_yatay):
    gruplar = [dict(r) for r in bolgeler]
    degisti = True
    while degisti:
        degisti = False
        for i in range(len(gruplar)):
            if gruplar[i] is None:
                continue
            for j in range(i + 1, len(gruplar)):
                if gruplar[j] is None:
                    continue
                a, b = gruplar[i], gruplar[j]
                aR, aB = a['x'] + a['w'], a['y'] + a['h']
                bR, bB = b['x'] + b['w'], b['y'] + b['h']
                dikey = max(0, max(a['y'] - bB, b['y'] - aB))
                ustuste = a['x'] < bR and b['x'] < aR
                yatay = max(0, max(a['x'] - bR, b['x'] - aR))
                if dikey <= max_dikey and (ustuste or yatay <= max_yatay):
                    nx, ny = min(a['x'], b['x']), min(a['y'], b['y'])
                    nr, nb = max(aR, bR), max(aB, bB)
                    gruplar[i] = {'x': nx, 'y': ny, 'w': nr - nx, 'h': nb - ny, 'area': a['area'] + b['area']}
                    gruplar[j] = None
                    degisti = True
        gruplar = [g for g in gruplar if g is not None]
    return gruplar

def tahta_onizleme(page, r, zoom=TAHTA_ZOOM):
    try:
        clip = fitz.Rect(r['x'] / zoom, r['y'] / zoom, (r['x'] + r['w']) / zoom, (r['y'] + r['h']) / zoom)
        kel = sorted(page.get_text("words", clip=clip), key=lambda w: (round(w[1] / 4), w[0]))
        metin = " ".join(w[4] for w in kel)[:110]
        if metin:
            return metin
    except Exception:
        pass
    return f"({r['w']}x{r['h']})"

def tahta_sayfa_farklari(img_s, img_t, page_t, esik, grup):
    mask = tahta_fark_maskesi(img_s, img_t, esik)
    if not mask.any():
        return []
    genis = tahta_genislet(mask, 7)
    bolgeler = tahta_bilesenler(genis, 250, 22, 10)
    if not bolgeler:
        return []
    cikis = []
    W, H = img_t.size
    for r in tahta_grupla(bolgeler, grup, 90):
        pad = 2
        rx, ry = max(0, r['x'] - pad), max(0, r['y'] - pad)
        rw, rh = min(W - rx, r['w'] + pad * 2), min(H - ry, r['h'] + pad * 2)
        cikis.append({'x': rx, 'y': ry, 'w': rw, 'h': rh,
                      'crop': img_t.crop((rx, ry, rx + rw, ry + rh)),
                      'preview': tahta_onizleme(page_t, {'x': rx, 'y': ry, 'w': rw, 'h': rh})})
    cikis.sort(key=lambda c: (c['y'], c['x']))
    return cikis


class AkilliTahtaPenceresi(tk.Toplevel):
    BG, KOYU, SIDE, TURUNCU, YESIL, MAVI = "#1e1e1e", "#111111", "#252525", "#ff8c00", "#2e7d32", "#4fc3f7"

    def __init__(self, ana):
        super().__init__(ana.root)
        self.ana = ana
        self.title("Akilli Tahta - Ogrenci / Ogretmen PDF Farklari")
        self.geometry("1320x880")
        self.configure(bg=self.BG)
        self.ogrenci = None
        self.ogretmen = None
        self.kilit = threading.Lock()
        self.kuyruk = queue.Queue()
        self.calisiyor = False
        self.items = []
        self.sayfa = 1
        self.toplam = 0
        self.secili = None
        self.gorunum = 1.0
        self.ofs = (20, 20)
        self._img_onbellek = (None, None)
        self._sayfa_tk = None
        self._tk_refs = []
        self._yeniden_cizim_id = None
        self._son_genislik = 0
        self._arayuz()
        self.bind("<Left>", lambda e: self.sayfa_degistir(-1))
        self.bind("<Right>", lambda e: self.sayfa_degistir(1))
        self.bind("<F11>", self.tam_ekran)
        self.bind("<Escape>", lambda e: self.attributes("-fullscreen", False))
        self.protocol("WM_DELETE_WINDOW", self.kapat)
        self.after(100, self._kuyruk_oku)

    def _arayuz(self):
        ust = tk.Frame(self, bg=self.KOYU, highlightthickness=0)
        ust.pack(side=tk.TOP, fill=tk.X)
        tk.Frame(self, bg=self.TURUNCU, height=2).pack(side=tk.TOP, fill=tk.X)
        tk.Label(ust, text="Akilli Tahta", bg=self.KOYU, fg=self.TURUNCU, font=("Segoe UI", 12, "bold")).pack(side=tk.LEFT, padx=10, pady=8)
        stil = dict(bg="#333333", fg="#eeeeee", relief=tk.FLAT, padx=10, pady=4, font=("Segoe UI", 9, "bold"), activebackground="#444444", activeforeground="white")
        self.ogr_btn = tk.Button(ust, text="Ogrenci PDF", command=lambda: self.pdf_sec('ogrenci'), **stil)
        self.ogr_btn.pack(side=tk.LEFT, padx=3)
        self.ogt_btn = tk.Button(ust, text="Ogretmen PDF", command=lambda: self.pdf_sec('ogretmen'), **stil)
        self.ogt_btn.pack(side=tk.LEFT, padx=3)
        self.bul_btn = tk.Button(ust, text="Farklari Bul", command=self.farklari_bul, state=tk.DISABLED,
                                 bg=self.TURUNCU, fg="#111111", relief=tk.FLAT, padx=12, pady=4, font=("Segoe UI", 9, "bold"),
                                 disabledforeground="#999999")
        self.bul_btn.pack(side=tk.LEFT, padx=6)
        self.esik_var = tk.IntVar(value=80)
        self.grup_var = tk.IntVar(value=45)
        for ad, var, lo, hi in (("Fark esigi", self.esik_var, 40, 200), ("Gruplama", self.grup_var, 15, 90)):
            tk.Label(ust, text=ad + ":", bg=self.KOYU, fg="#bbbbbb", font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=(8, 0))
            tk.Scale(ust, from_=lo, to=hi, orient=tk.HORIZONTAL, variable=var, length=90, showvalue=True,
                     bg=self.KOYU, fg="#bbbbbb", troughcolor="#333333", highlightthickness=0, bd=0, sliderlength=14).pack(side=tk.LEFT)
        for metin, komut in (("-", lambda: self.yakinlastir(1 / 1.15)), ("+", lambda: self.yakinlastir(1.15)), ("Sigdir", self.sigdir), ("Tam Ekran (F11)", self.tam_ekran)):
            tk.Button(ust, text=metin, command=komut, **stil).pack(side=tk.LEFT, padx=2)
        self.durum_lbl = tk.Label(ust, text="PDF'leri secin", bg=self.KOYU, fg=self.MAVI, font=("Segoe UI", 9))
        self.durum_lbl.pack(side=tk.RIGHT, padx=12)

        ana = tk.Frame(self, bg=self.BG)
        ana.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        yan = tk.Frame(ana, bg=self.SIDE, width=340)
        yan.pack(side=tk.LEFT, fill=tk.Y)
        yan.pack_propagate(False)
        nav = tk.Frame(yan, bg="#2a2a2a")
        nav.pack(side=tk.TOP, fill=tk.X)
        nstil = dict(bg=self.TURUNCU, fg="#111111", relief=tk.FLAT, padx=12, font=("Segoe UI", 9, "bold"), disabledforeground="#999999")
        self.onceki_btn = tk.Button(nav, text="<", command=lambda: self.sayfa_degistir(-1), **nstil)
        self.onceki_btn.pack(side=tk.LEFT, padx=8, pady=8)
        self.sayfa_lbl = tk.Label(nav, text="- / -", bg="#2a2a2a", fg="#eeeeee", font=("Segoe UI", 10))
        self.sayfa_lbl.pack(side=tk.LEFT, expand=True)
        self.sonraki_btn = tk.Button(nav, text=">", command=lambda: self.sayfa_degistir(1), **nstil)
        self.sonraki_btn.pack(side=tk.RIGHT, padx=8, pady=8)
        bas = tk.Frame(yan, bg=self.SIDE)
        bas.pack(side=tk.TOP, fill=tk.X, padx=8, pady=6)
        self.sayac_lbl = tk.Label(bas, text="Farklar (0)", bg=self.SIDE, fg=self.TURUNCU, font=("Segoe UI", 10, "bold"))
        self.sayac_lbl.pack(side=tk.LEFT)
        tk.Button(bas, text="Tumu Ac/Kapat", command=self.tumunu_ac_kapat, bg="#333333", fg="#eeeeee", relief=tk.FLAT, font=("Segoe UI", 8)).pack(side=tk.RIGHT)
        lf = tk.Frame(yan, bg=self.SIDE)
        lf.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=6, pady=(0, 6))
        sb = tk.Scrollbar(lf)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.liste = tk.Listbox(lf, bg="#2d2d2d", fg="#cccccc", selectbackground="#2a3a4a", selectforeground="white",
                                highlightthickness=0, bd=0, activestyle="none", font=("Segoe UI", 9), yscrollcommand=sb.set, exportselection=False)
        self.liste.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.config(command=self.liste.yview)
        self.liste.bind("<<ListboxSelect>>", self._liste_secildi)

        gf = tk.Frame(ana, bg=self.BG)
        gf.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.canvas = tk.Canvas(gf, bg=self.BG, highlightthickness=0)
        sy = tk.Scrollbar(gf, orient=tk.VERTICAL, command=self.canvas.yview)
        sx = tk.Scrollbar(gf, orient=tk.HORIZONTAL, command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)
        sy.pack(side=tk.RIGHT, fill=tk.Y)
        sx.pack(side=tk.BOTTOM, fill=tk.X)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.canvas.bind("<MouseWheel>", lambda e: self.canvas.yview_scroll(-1 if e.delta > 0 else 1, "units"))
        self.canvas.bind("<Shift-MouseWheel>", lambda e: self.canvas.xview_scroll(-1 if e.delta > 0 else 1, "units"))
        self.canvas.bind("<Button-4>", lambda e: self.canvas.yview_scroll(-1, "units"))
        self.canvas.bind("<Button-5>", lambda e: self.canvas.yview_scroll(1, "units"))
        self.canvas.bind("<Configure>", self._canvas_boyut)
        self.nav_guncelle()

    def durum(self, metin):
        self.durum_lbl.config(text=metin)

    def pdf_sec(self, tur):
        if self.calisiyor:
            return
        bas = os.path.dirname(self.ana.pdf_yolu) if getattr(self.ana, 'pdf_yolu', None) else None
        yol = filedialog.askopenfilename(parent=self, title="Ogrenci PDF" if tur == 'ogrenci' else "Ogretmen PDF",
                                         initialdir=bas, filetypes=[("PDF", "*.pdf")])
        if not yol:
            return
        self.durum(("Ogrenci" if tur == 'ogrenci' else "Ogretmen") + " PDF okunuyor...")
        self.update_idletasks()
        try:
            doc = fitz.open(yol)
        except Exception as e:
            self.durum("Hata: " + str(e))
            return
        with self.kilit:
            eski = getattr(self, tur)
            setattr(self, tur, doc)
            if eski is not None:
                try: eski.close()
                except Exception: pass
        btn, ad = (self.ogr_btn, "Ogrenci") if tur == 'ogrenci' else (self.ogt_btn, "Ogretmen")
        btn.config(text=f"{ad} ({len(doc)} sf): {os.path.basename(yol)[:22]}", bg=self.YESIL)
        self._sonuclari_sifirla()
        if tur == 'ogrenci':
            self.toplam, self.sayfa = len(doc), 1
            self._img_onbellek = (None, None)
            self.sayfa_ciz()
        self.hazir_mi()

    def hazir_mi(self):
        ok = self.ogrenci is not None and self.ogretmen is not None
        self.bul_btn.config(state=tk.NORMAL if ok and not self.calisiyor else tk.DISABLED)
        if ok:
            self.durum('Hazir - "Farklari Bul"a basin')
        elif self.ogrenci:
            self.durum("Ogretmen PDF'ini secin")
        elif self.ogretmen:
            self.durum("Ogrenci PDF'ini secin")

    def _sonuclari_sifirla(self):
        self.items = []
        self.secili = None
        self.liste.delete(0, tk.END)
        self.sayac_lbl.config(text="Farklar (0)")
        if self.ogrenci is not None:
            self.sayfa_ciz()

    def farklari_bul(self):
        if self.calisiyor or not (self.ogrenci and self.ogretmen):
            return
        try:
            import numpy  # noqa
        except ImportError:
            messagebox.showerror("Eksik paket", "Fark bulmak icin numpy gerekli:\n\npip install numpy", parent=self)
            return
        self.calisiyor = True
        self.bul_btn.config(state=tk.DISABLED)
        esik, grup = int(self.esik_var.get()), int(self.grup_var.get())
        threading.Thread(target=self._bul_is, args=(esik, grup), daemon=True).start()

    def _bul_is(self, esik, grup):
        sonuc, atlanan = [], []
        try:
            n = min(len(self.ogrenci), len(self.ogretmen))
            for p in range(n):
                self.kuyruk.put(('durum', f"Sayfa {p + 1}/{n} karsilastiriliyor..."))
                try:
                    with self.kilit:
                        a = _tahta_render(self.ogrenci, p)
                        b = _tahta_render(self.ogretmen, p)
                        if a.size != b.size:
                            atlanan.append(p + 1)
                            continue
                        for f in tahta_sayfa_farklari(a, b, self.ogretmen.load_page(p), esik, grup):
                            f['page'] = p + 1
                            f['acik'] = False
                            sonuc.append(f)
                except Exception as e:
                    print("Akilli Tahta sayfa hatasi", p + 1, e)
            self.kuyruk.put(('bitti', sonuc, atlanan))
        except Exception as e:
            self.kuyruk.put(('hata', str(e)))

    def _kuyruk_oku(self):
        try:
            while True:
                m = self.kuyruk.get_nowait()
                if m[0] == 'durum':
                    self.durum(m[1])
                elif m[0] == 'bitti':
                    self._bul_bitti(m[1], m[2])
                elif m[0] == 'hata':
                    self.calisiyor = False
                    self.durum("Hata: " + m[1])
                    self.hazir_mi()
        except queue.Empty:
            pass
        try:
            self.after(100, self._kuyruk_oku)
        except tk.TclError:
            pass

    def _bul_bitti(self, sonuc, atlanan):
        self.calisiyor = False
        for i, it in enumerate(sonuc):
            it['id'] = i
        self.items = sonuc
        self.secili = None
        self.toplam = len(self.ogrenci)
        self.sayfa = 1
        self.liste_doldur()
        self.bul_btn.config(state=tk.NORMAL)
        ek = f" ({len(atlanan)} sayfa boyut farki nedeniyle atlandi: {atlanan[:6]})" if atlanan else ""
        self.durum(f"{len(sonuc)} fark bulundu{ek}")
        self.sayfa_ciz()

    def liste_doldur(self):
        self.liste.delete(0, tk.END)
        for i, it in enumerate(self.items):
            self.liste.insert(tk.END, f"S.{it['page']} #{i + 1}   {it['preview']}")
        self.sayac_lbl.config(text=f"Farklar ({len(self.items)})")
        if not self.items:
            self.liste.insert(tk.END, "Fark bulunamadi. Fark esigini dusurup tekrar deneyin.")

    def _liste_secildi(self, _e=None):
        s = self.liste.curselection()
        if not s or s[0] >= len(self.items):
            return
        it = self.items[s[0]]
        if it['page'] != self.sayfa:
            self.sayfa = it['page']
            self.sayfa_ciz()
        self.sec(it, listeyi_guncelle=False)
        self._goster(it)

    def sec(self, it, listeyi_guncelle=True):
        self.secili = it
        for x in self.items:
            self._marker_gorunum(x)
        if listeyi_guncelle:
            self.liste.selection_clear(0, tk.END)
            self.liste.selection_set(self.items.index(it))
            self.liste.see(self.items.index(it))

    def _goster(self, it):
        ids = it.get('_marker')
        if not ids:
            return
        self.update_idletasks()
        x1, y1, _, _ = self.canvas.bbox(ids[0])
        sr = self.canvas.cget("scrollregion").split()
        if len(sr) == 4:
            tw, th = max(1, float(sr[2])), max(1, float(sr[3]))
            self.canvas.xview_moveto(max(0, (x1 - self.canvas.winfo_width() / 2) / tw))
            self.canvas.yview_moveto(max(0, (y1 - self.canvas.winfo_height() / 2) / th))

    def tumunu_ac_kapat(self):
        if not self.items:
            return
        ac = any(not it['acik'] for it in self.items)
        for it in self.items:
            it['acik'] = ac
            self._marker_gorunum(it)

    def sayfa_degistir(self, d):
        yeni = self.sayfa + d
        if not self.ogrenci or yeni < 1 or yeni > self.toplam:
            return
        self.sayfa = yeni
        self.sayfa_ciz()
        self.canvas.yview_moveto(0)

    def nav_guncelle(self):
        self.sayfa_lbl.config(text=f"{self.sayfa} / {self.toplam}" if self.toplam else "- / -")
        self.onceki_btn.config(state=tk.NORMAL if self.toplam and self.sayfa > 1 else tk.DISABLED)
        self.sonraki_btn.config(state=tk.NORMAL if self.toplam and self.sayfa < self.toplam else tk.DISABLED)

    def _sayfa_img(self):
        if self._img_onbellek[0] != self.sayfa:
            with self.kilit:
                img = _tahta_render(self.ogrenci, self.sayfa - 1)
            self._img_onbellek = (self.sayfa, img)
        return self._img_onbellek[1]

    def yakinlastir(self, carpan):
        self.gorunum = min(4.0, max(0.25, self.gorunum * carpan))
        self.sayfa_ciz()

    def sigdir(self):
        if not self.ogrenci:
            return
        img = self._sayfa_img()
        self.gorunum = max(0.25, (self.canvas.winfo_width() - 40) / img.width)
        self.sayfa_ciz()

    def tam_ekran(self, _e=None):
        self.attributes("-fullscreen", not bool(self.attributes("-fullscreen")))

    def _canvas_boyut(self, e):
        if abs(e.width - self._son_genislik) < 4:
            return
        self._son_genislik = e.width
        if self._yeniden_cizim_id:
            self.after_cancel(self._yeniden_cizim_id)
        self._yeniden_cizim_id = self.after(150, self.sayfa_ciz)

    def sayfa_ciz(self):
        self._yeniden_cizim_id = None
        c = self.canvas
        c.delete("all")
        self._tk_refs = []
        for it in self.items:
            it.pop('_marker', None)
            it.pop('_ov', None)
        self.nav_guncelle()
        if not self.ogrenci:
            return
        k = self.gorunum
        img = self._sayfa_img()
        goster = img if abs(k - 1) < 1e-6 else img.resize((max(1, int(img.width * k)), max(1, int(img.height * k))), Image.LANCZOS)
        ox = max(20, (c.winfo_width() - goster.width) // 2)
        self.ofs = (ox, 20)
        self._sayfa_tk = ImageTk.PhotoImage(goster)
        c.create_image(ox, 20, image=self._sayfa_tk, anchor="nw", tags="sayfa")
        c.config(scrollregion=(0, 0, ox + goster.width + 20, goster.height + 40))
        sayfa_items = [it for it in self.items if it['page'] == self.sayfa]
        for it in sayfa_items:
            self._ovr_ciz(it)
        for it in sayfa_items:
            self._marker_ciz(it)

    def _ovr_ciz(self, it):
        k, (ox, oy) = self.gorunum, self.ofs
        crop = it['crop']
        if abs(k - 1) > 1e-6:
            crop = crop.resize((max(1, int(it['w'] * k)), max(1, int(it['h'] * k))), Image.LANCZOS)
        tkimg = ImageTk.PhotoImage(crop)
        self._tk_refs.append(tkimg)
        it['_ov'] = self.canvas.create_image(ox + it['x'] * k, oy + it['y'] * k, image=tkimg, anchor="nw",
                                             state=tk.NORMAL if it['acik'] else tk.HIDDEN, tags=(f"g{it['id']}",))

    def _marker_ciz(self, it):
        k, (ox, oy) = self.gorunum, self.ofs
        x, y = ox + it['x'] * k, oy + it['y'] * k
        mx = x - 28
        if mx < 2:
            mx = x + 2
        my = y + 2
        c = self.canvas
        g, m = f"g{it['id']}", f"m{it['id']}"
        halka = c.create_oval(mx - 4, my - 4, mx + 26, my + 26, outline=self.MAVI, width=3, state=tk.HIDDEN, tags=(g, m))
        oval = c.create_oval(mx, my, mx + 22, my + 22, fill=self.TURUNCU, outline="white", width=2, tags=(g, m))
        yazi = c.create_text(mx + 11, my + 11, text="?", fill="white", font=("Segoe UI", 10, "bold"), tags=(g, m))
        it['_marker'] = (oval, yazi, halka)
        self._marker_gorunum(it)
        c.tag_bind(m, "<ButtonPress-1>", lambda e, it=it: self._m_bas(e, it))
        c.tag_bind(m, "<B1-Motion>", lambda e, it=it: self._m_surukle(e, it))
        c.tag_bind(m, "<ButtonRelease-1>", lambda e, it=it: self._m_birak(e, it))
        c.tag_bind(m, "<Enter>", lambda e: c.config(cursor="hand2"))
        c.tag_bind(m, "<Leave>", lambda e: c.config(cursor=""))

    def _marker_gorunum(self, it):
        ids = it.get('_marker')
        if not ids:
            return
        oval, yazi, halka = ids
        c = self.canvas
        c.itemconfigure(oval, fill=self.YESIL if it['acik'] else self.TURUNCU)
        c.itemconfigure(yazi, text="\u2713" if it['acik'] else "?")
        c.itemconfigure(halka, state=tk.NORMAL if self.secili is it else tk.HIDDEN)
        if it.get('_ov'):
            c.itemconfigure(it['_ov'], state=tk.NORMAL if it['acik'] else tk.HIDDEN)

    def _m_bas(self, e, it):
        it['_s'] = (self.canvas.canvasx(e.x), self.canvas.canvasy(e.y), False)

    def _m_surukle(self, e, it):
        sx, sy, surukledi = it['_s']
        cx, cy = self.canvas.canvasx(e.x), self.canvas.canvasy(e.y)
        if not surukledi and abs(cx - sx) <= 3 and abs(cy - sy) <= 3:
            return
        dx, dy = cx - sx, cy - sy
        self.canvas.move(f"g{it['id']}", dx, dy)
        k = self.gorunum
        it['x'] += dx / k
        it['y'] += dy / k
        it['_s'] = (cx, cy, True)

    def _m_birak(self, e, it):
        if not it['_s'][2]:
            it['acik'] = not it['acik']
            self.sec(it)
            self._marker_gorunum(it)

    def kapat(self):
        with self.kilit:
            for d in (self.ogrenci, self.ogretmen):
                try:
                    if d is not None: d.close()
                except Exception:
                    pass
            self.ogrenci = self.ogretmen = None
        self.destroy()


class PDFTopluSoruIsaretleyici:
    MIN_ZOOM, MAX_ZOOM = 0.2, 6.0
    TUTMA = 12
    PANEL_GENISLIK = 470
    MARJ_TUTMA = 9

    def __init__(self, root):
        self.root = root
        self.root.title("PDF Toplu Soru Isaretleyici")
        self.root.geometry("1500x950")
        self.pdf_yolu = None
        self.pdf_doc = None
        self.pdf_kilit = threading.Lock()
        self.toplam_sayfa = 0
        self.aktif_sayfa = 0
        self.tum_kutular = {}
        self.sayfa_bilgi = {}
        self.testler = []
        self.test_zorla = {}
        self.olcek = 1.0
        self.sigdir_modu = True
        self.img_tk = None

        self.secili = None
        self.secili_ortak = False
        self.ortak_aktif = None
        self.ortak_surukle = None
        self.cizim = None
        self.ilk_no_var = tk.IntVar(value=1)
        self.surukleme_modu = None
        self.surukleme_baslangic = (0, 0)
        self.kutu_baslangic = None
        self._fit_job = None
        self._imlec = ""

        self.marj_varsayilan_tek = dict(MARJ_VARSAYILAN_TEK)
        self.marj_varsayilan_cift = dict(MARJ_VARSAYILAN_CIFT)
        self.marj_sayfa = {}
        self.baslik_marj_varsayilan_tek = dict(MARJ_BASLIK_VARSAYILAN_TEK)
        self.baslik_marj_varsayilan_cift = dict(MARJ_BASLIK_VARSAYILAN_CIFT)
        self.baslik_marj_sayfa = {}

        self.marj_surukle = None
        self.secenekli_sart = tk.BooleanVar(value=True)
        self.etkinlik_bul = tk.BooleanVar(value=True)
        self.raster_pdf = tk.BooleanVar(value=False)
        self.cevap_gizle = tk.BooleanVar(value=True)
        self.cevap_satiri_sil = tk.BooleanVar(value=True)
        self.cozum_modu = tk.BooleanVar(value=False)
        self.ortak_mod = tk.BooleanVar(value=False)
        self.marj_goster = tk.BooleanVar(value=False)
        self.marj_sayfa_ozel = tk.BooleanVar(value=False)
        self.baslik_marj_goster = tk.BooleanVar(value=False)
        self.baslik_marj_sayfa_ozel = tk.BooleanVar(value=False)
        self.kuyruk = queue.Queue()
        self.arayuz_olustur()
        self.root.after(50, self._kuyruk_isle)

    def arayuz_olustur(self):
        ust = tk.Frame(self.root, bd=2, relief=tk.RAISED)
        ust.pack(side=tk.TOP, fill=tk.X, padx=5, pady=(5, 2))
        tk.Button(ust, text="PDF Ac", command=self.pdf_ac, bg="#4CAF50", fg="white", padx=10).pack(side=tk.LEFT, padx=3)
        self.tara_btn = tk.Button(ust, text="Tum Sayfalari Tara", command=self.tum_sayfalari_tara_threaded, bg="#2196F3", fg="white", padx=10)
        self.tara_btn.pack(side=tk.LEFT, padx=3)
        tk.Checkbutton(ust, text="Sadece sikli (A-E) sorular", variable=self.secenekli_sart).pack(side=tk.LEFT, padx=4)
        tk.Checkbutton(ust, text="Etkinlik bloklari", variable=self.etkinlik_bul).pack(side=tk.LEFT, padx=4)
        tk.Checkbutton(ust, text="\U0001F50D Cozum Modu", variable=self.cozum_modu, command=self._cozum_modu_degisti, bg="#E1BEE7", activebackground="#CE93D8").pack(side=tk.LEFT, padx=4)
        tk.Checkbutton(ust, text="\u25A3 Ortak Alan Modu", variable=self.ortak_mod, command=self._ortak_mod_degisti, bg="#FFE082", activebackground="#FFCA28").pack(side=tk.LEFT, padx=4)

        mb = tk.Frame(self.root, bd=1, relief=tk.RAISED)
        mb.pack(side=tk.TOP, fill=tk.X, padx=5, pady=(0, 3))
        ic = tk.Frame(mb)
        ic.pack(side=tk.TOP, fill=tk.X, padx=4, pady=2)
        tk.Label(ic, text="\u25A0 Icerik Marji:", font=("Arial", 9, "bold"), fg="#00838F").pack(side=tk.LEFT)
        tk.Checkbutton(ic, text="Goster", variable=self.marj_goster, command=self._marj_degisti, fg="#00838F").pack(side=tk.LEFT, padx=3)
        tk.Checkbutton(ic, text="\u2605 Bu Sayfaya Ozel", variable=self.marj_sayfa_ozel, command=self._marj_degisti, fg="#E65100").pack(side=tk.LEFT, padx=3)
        tk.Button(ic, text="\u21ba Bu Sayfa -> Varsayilan", command=self.marj_bu_sayfa_sil, bg="#607D8B", fg="white", padx=6, font=("Arial", 8)).pack(side=tk.LEFT, padx=2)
        tk.Button(ic, text="\u21ba Aktif Grubu Sifirla", command=self.marj_sifirla, bg="#795548", fg="white", padx=6, font=("Arial", 8)).pack(side=tk.LEFT, padx=2)

        bs = tk.Frame(mb)
        bs.pack(side=tk.TOP, fill=tk.X, padx=4, pady=2)
        tk.Label(bs, text="\u25A0 Baslik Marji:", font=("Arial", 9, "bold"), fg="#AD1457").pack(side=tk.LEFT)
        tk.Checkbutton(bs, text="Goster", variable=self.baslik_marj_goster, command=self._marj_degisti, fg="#AD1457").pack(side=tk.LEFT, padx=3)
        tk.Checkbutton(bs, text="\u2605 Bu Sayfaya Ozel", variable=self.baslik_marj_sayfa_ozel, command=self._marj_degisti, fg="#E65100").pack(side=tk.LEFT, padx=3)
        tk.Button(bs, text="\u21ba Bu Sayfa -> Varsayilan", command=self.baslik_marj_bu_sayfa_sil, bg="#607D8B", fg="white", padx=6, font=("Arial", 8)).pack(side=tk.LEFT, padx=2)
        tk.Button(bs, text="\u21ba Aktif Grubu Sifirla", command=self.baslik_marj_sifirla, bg="#795548", fg="white", padx=6, font=("Arial", 8)).pack(side=tk.LEFT, padx=2)

        alt_bar = tk.Frame(self.root, bd=1, relief=tk.RAISED)
        alt_bar.pack(side=tk.TOP, fill=tk.X, padx=5)
        tk.Button(alt_bar, text="Cevap Anahtari", command=self.cevap_anahtari_goster, bg="#009688", fg="white", padx=10).pack(side=tk.LEFT, padx=3, pady=3)
        tk.Button(alt_bar, text="JSON Kaydet", command=self.kutulari_kaydet, bg="#FF9800", fg="white", padx=10).pack(side=tk.LEFT, padx=3, pady=3)
        tk.Button(alt_bar, text="JSON Yukle", command=self.kutulari_yukle, bg="#9C27B0", fg="white", padx=10).pack(side=tk.LEFT, padx=3, pady=3)
        tk.Button(alt_bar, text="Akilli Tahta", command=self.akilli_tahta_ac, bg="#00796B", fg="white", padx=10).pack(side=tk.LEFT, padx=3, pady=3)
        tk.Button(alt_bar, text="Sayfa Tani", command=self.sayfa_tani, bg="#455A64", fg="white", padx=10).pack(side=tk.LEFT, padx=3, pady=3)
        tk.Button(alt_bar, text="Tumunu Temizle", command=self.her_seyi_temizle, bg="#f44336", fg="white", padx=10).pack(side=tk.LEFT, padx=3, pady=3)

        zm = tk.Frame(alt_bar)
        zm.pack(side=tk.LEFT, padx=12)
        tk.Button(zm, text=" - ", command=lambda: self.yakinlastir(1 / 1.25)).pack(side=tk.LEFT)
        self.zoom_lbl = tk.Label(zm, text="100%", width=6, font=("Arial", 10, "bold"))
        self.zoom_lbl.pack(side=tk.LEFT)
        tk.Button(zm, text=" + ", command=lambda: self.yakinlastir(1.25)).pack(side=tk.LEFT)
        tk.Button(zm, text="Sigdir", command=self.sigdir).pack(side=tk.LEFT, padx=(6, 0))

        nav = tk.Frame(alt_bar)
        nav.pack(side=tk.RIGHT, padx=5)
        tk.Button(nav, text="<< Onceki", command=self.onceki_sayfa, padx=8).pack(side=tk.LEFT)
        self.sayfa_lbl = tk.Label(nav, text="Sayfa: - / -", width=16, font=("Arial", 11, "bold"))
        self.sayfa_lbl.pack(side=tk.LEFT, padx=8)
        tk.Button(nav, text="Sonraki >>", command=self.sonraki_sayfa, padx=8).pack(side=tk.LEFT)

        bilgi = tk.Frame(self.root)
        bilgi.pack(side=tk.TOP, fill=tk.X, padx=5, pady=(2, 5))
        self.durum_lbl = tk.Label(bilgi, text="Hazir. PDF acin ve Tara butonuna basin.", anchor="w", fg="#333")
        self.durum_lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.ilerme = ttk.Progressbar(bilgi, orient="horizontal", length=250, mode="determinate")
        self.ilerme.pack(side=tk.RIGHT, padx=5)

        alt = tk.Frame(self.root, bd=1, relief=tk.SUNKEN)
        alt.pack(side=tk.BOTTOM, fill=tk.X)
        tk.Label(alt, text="Sol tik: sec/tasi | Sag tik: sil | Cift tik: yeni kutu (Ortak Alan Modu acikken: yeni ortak alan) | + Yeni Ortak Alan: sonra bos yere surukleyip/tiklayip sorulari ekleyin, Esc ile bitirin",
                 anchor="w", fg="#666", font=("Arial", 9)).pack(side=tk.LEFT, padx=5)

        govde = tk.Frame(self.root)
        govde.pack(fill=tk.BOTH, expand=True)
        self.panel_olustur(govde)

        sol = tk.Frame(govde)
        sol.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        h_sb = tk.Scrollbar(sol, orient=tk.HORIZONTAL)
        h_sb.pack(side=tk.BOTTOM, fill=tk.X)
        v_sb = tk.Scrollbar(sol, orient=tk.VERTICAL)
        v_sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.canvas = tk.Canvas(sol, bg="#2b2b2b", cursor="crosshair", highlightthickness=0)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        v_sb.config(command=self.canvas.yview)
        h_sb.config(command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=v_sb.set, xscrollcommand=h_sb.set)

        self.canvas.bind("<Button-1>", self.fare_tikla)
        self.canvas.bind("<B1-Motion>", self.fare_surukle)
        self.canvas.bind("<ButtonRelease-1>", self.fare_birak)
        self.canvas.bind("<Motion>", self.fare_gez)
        self.canvas.bind("<Leave>", lambda e: self._imlec_ayarla(None))
        self.canvas.bind("<Button-3>", self.sag_tikla)
        self.canvas.bind("<Double-Button-1>", self.cift_tikla)
        self.canvas.bind("<Configure>", self.pencere_boyutu_degisti)
        self.canvas.bind("<MouseWheel>", self.tekerlek)
        self.canvas.bind("<Control-MouseWheel>", self.tekerlek_zoom)
        self.canvas.bind("<Shift-MouseWheel>", self.tekerlek_yatay)
        self.canvas.bind("<Button-4>", lambda e: self.tekerlek(e, 1))
        self.canvas.bind("<Button-5>", lambda e: self.tekerlek(e, -1))
        self.canvas.bind("<Control-Button-4>", lambda e: self.tekerlek_zoom(e, 1))
        self.canvas.bind("<Control-Button-5>", lambda e: self.tekerlek_zoom(e, -1))

        self.root.bind("<Left>", lambda e: self.onceki_sayfa())
        self.root.bind("<Right>", lambda e: self.sonraki_sayfa())
        self.root.bind("<Escape>", lambda e: self.ortak_ekle_bitir())
        self.root.bind("<Delete>", self.secili_sil)
        self.root.bind("<BackSpace>", self.secili_sil)

    def panel_olustur(self, ebeveyn):
        panel = tk.Frame(ebeveyn, width=self.PANEL_GENISLIK, bd=1, relief=tk.GROOVE)
        panel.pack(side=tk.RIGHT, fill=tk.Y, padx=(4, 0))
        panel.pack_propagate(False)
        self.liste_baslik = tk.Label(panel, text="Testler", font=("Arial", 11, "bold"), anchor="w")
        self.liste_baslik.pack(side=tk.TOP, fill=tk.X, padx=6, pady=(6, 2))

        self.ortak_panel = tk.LabelFrame(panel, text="Secili Ortak Alan / Soru Ayarlari")
        self.ortak_panel.pack(side=tk.TOP, fill=tk.X, padx=6, pady=4)

        self.ortak_nums_lbl = tk.Label(self.ortak_panel, text="Sorular: -", anchor="w")
        self.ortak_nums_lbl.pack(side=tk.TOP, fill=tk.X, padx=4, pady=2)

        btn_frame = tk.Frame(self.ortak_panel)
        btn_frame.pack(side=tk.TOP, fill=tk.X, padx=4, pady=2)

        tk.Button(btn_frame, text="Ortak Alani Duzenle", command=self.ortak_alan_duzenle_dialog).pack(side=tk.LEFT, expand=True, fill=tk.X, pady=(0,4))

        btn_ekle_frame = tk.Frame(self.ortak_panel)
        btn_ekle_frame.pack(side=tk.TOP, fill=tk.X, padx=4, pady=(2,4))
        tk.Button(btn_ekle_frame, text="+ Yeni Soru Kutusu", command=lambda: self.yeni_kutu_ekle(False)).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=1)
        tk.Button(btn_ekle_frame, text="+ Yeni Ortak Alan", command=lambda: self.yeni_kutu_ekle(True)).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=1)

        self.ortak_ekle_cerceve = tk.LabelFrame(self.ortak_panel, text="Ortak alana fareyle soru ekleme")
        self.ortak_ekle_cerceve.pack(side=tk.TOP, fill=tk.X, padx=4, pady=(2, 4))
        oe1 = tk.Frame(self.ortak_ekle_cerceve)
        oe1.pack(side=tk.TOP, fill=tk.X, padx=2, pady=2)
        tk.Label(oe1, text="Ilk soru no:").pack(side=tk.LEFT)
        self.ilk_no_spin = tk.Spinbox(oe1, from_=1, to=999, width=5, textvariable=self.ilk_no_var, command=self._ilk_no_degisti)
        self.ilk_no_spin.pack(side=tk.LEFT, padx=3)
        self.ilk_no_spin.bind("<Return>", lambda e: self._ilk_no_degisti())
        self.ilk_no_spin.bind("<FocusOut>", lambda e: self._ilk_no_degisti())
        tk.Button(oe1, text="Bitir (Esc)", command=self.ortak_ekle_bitir, bg="#FFCA28").pack(side=tk.RIGHT, padx=2)
        tk.Button(oe1, text="Secili ortak alana soru ekle", command=self.secili_ortaga_soru_ekle_baslat).pack(side=tk.RIGHT, padx=2)
        self.ortak_ekle_lbl = tk.Label(self.ortak_ekle_cerceve, text="Pasif. '+ Yeni Ortak Alan' ile baslayin.", anchor="w", justify=tk.LEFT, fg="#555", wraplength=420)
        self.ortak_ekle_lbl.pack(side=tk.TOP, fill=tk.X, padx=4, pady=(0, 3))

        zr = tk.LabelFrame(panel, text="Test sinirlari (yanlis bolunduyse elle duzelt)")
        zr.pack(side=tk.TOP, fill=tk.X, padx=6, pady=(4, 0))
        self.zorla_lbl = tk.Label(zr, text="Gecerli sayfa: -", anchor="w", fg="#333")
        self.zorla_lbl.pack(side=tk.TOP, fill=tk.X, padx=4)
        zb = tk.Frame(zr)
        zb.pack(side=tk.TOP, fill=tk.X, padx=4, pady=3)
        tk.Button(zb, text="Bu sayfa yeni test baslatir", command=lambda: self.sayfa_test_zorla(True)).pack(side=tk.LEFT, expand=True, fill=tk.X)
        tk.Button(zb, text="Oncekine ekle", command=lambda: self.sayfa_test_zorla(False)).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2)
        tk.Button(zb, text="Otomatik", command=lambda: self.sayfa_test_zorla(None)).pack(side=tk.LEFT, expand=True, fill=tk.X)

        btn = tk.Frame(panel)
        btn.pack(side=tk.BOTTOM, fill=tk.X, padx=6, pady=6)
        tk.Checkbutton(btn, text="Goruntu olarak kaydet (gizli metin kalmaz)", variable=self.raster_pdf, anchor="w").pack(side=tk.TOP, fill=tk.X)
        tk.Checkbutton(btn, text="Isaretli cevap dairelerini sil (cevapli PDF icin)", variable=self.cevap_gizle, anchor="w").pack(side=tk.TOP, fill=tk.X)
        tk.Checkbutton(btn, text="Cevabin okundugu satiri sil (cevap seridi / 'N DERS B 3902' satiri)", variable=self.cevap_satiri_sil, anchor="w").pack(side=tk.TOP, fill=tk.X)
        self.pdf_btn_secili = tk.Button(btn, text="Secili Testleri PDF Yap", bg="#673AB7", fg="white", command=lambda: self.pdf_uret(False))
        self.pdf_btn_secili.pack(side=tk.TOP, fill=tk.X, pady=(4, 2))
        self.pdf_btn_hepsi = tk.Button(btn, text="Tum Testleri PDF Yap", bg="#512DA8", fg="white", command=lambda: self.pdf_uret(True))
        self.pdf_btn_hepsi.pack(side=tk.TOP, fill=tk.X)

        kap = tk.Frame(panel)
        kap.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=6)
        self.test_agaci = ttk.Treeview(kap, columns=("konu", "soru", "cevap", "sayfa"), show="tree headings", selectmode="extended")
        self.test_agaci.tag_configure('uyari', background='#ffe0b2')
        self.test_agaci.heading("#0", text="Test")
        self.test_agaci.heading("konu", text="Konu")
        self.test_agaci.heading("soru", text="Soru")
        self.test_agaci.heading("cevap", text="Cevap")
        self.test_agaci.heading("sayfa", text="Sayfalar")
        self.test_agaci.column("#0", width=60, stretch=False)
        self.test_agaci.column("konu", width=130)
        self.test_agaci.column("soru", width=52, anchor="center", stretch=False)
        self.test_agaci.column("cevap", width=120, anchor="w")
        self.test_agaci.column("sayfa", width=110)
        sb = tk.Scrollbar(kap, orient=tk.VERTICAL, command=self.test_agaci.yview)
        self.test_agaci.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.test_agaci.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.test_agaci.bind("<Double-1>", self.teste_git)

    def _ui(self, fn): self.kuyruk.put(fn)
    def _kuyruk_isle(self):
        try:
            while True: self.kuyruk.get_nowait()()
        except queue.Empty: pass
        self.root.after(50, self._kuyruk_isle)

    def marj_al(self, sayfa):
        if sayfa in self.marj_sayfa: return self.marj_sayfa[sayfa]
        return self.marj_varsayilan_tek if _sayfa_tek_mi(sayfa) else self.marj_varsayilan_cift

    def baslik_marj_al(self, sayfa):
        if sayfa in self.baslik_marj_sayfa: return self.baslik_marj_sayfa[sayfa]
        return self.baslik_marj_varsayilan_tek if _sayfa_tek_mi(sayfa) else self.baslik_marj_varsayilan_cift

    def marj_sifirla(self):
        if _sayfa_tek_mi(self.aktif_sayfa): self.marj_varsayilan_tek = dict(MARJ_VARSAYILAN_TEK)
        else: self.marj_varsayilan_cift = dict(MARJ_VARSAYILAN_CIFT)
        self.marj_sayfa.pop(self.aktif_sayfa, None)
        self._marj_ciz()
        self._marj_durum_yaz()

    def marj_bu_sayfa_sil(self):
        if self.aktif_sayfa in self.marj_sayfa:
            self.marj_sayfa.pop(self.aktif_sayfa, None)
            self._marj_ciz()
            self._marj_durum_yaz()

    def baslik_marj_sifirla(self):
        if _sayfa_tek_mi(self.aktif_sayfa): self.baslik_marj_varsayilan_tek = dict(MARJ_BASLIK_VARSAYILAN_TEK)
        else: self.baslik_marj_varsayilan_cift = dict(MARJ_BASLIK_VARSAYILAN_CIFT)
        self.baslik_marj_sayfa.pop(self.aktif_sayfa, None)
        self.sayfa_bilgi = {}
        self._marj_ciz()
        self._marj_durum_yaz()

    def baslik_marj_bu_sayfa_sil(self):
        if self.aktif_sayfa in self.baslik_marj_sayfa:
            self.baslik_marj_sayfa.pop(self.aktif_sayfa, None)
            self.sayfa_bilgi.pop(self.aktif_sayfa, None)
            self._marj_ciz()
            self._marj_durum_yaz()

    def _marj_degisti(self): self._marj_ciz()

    def _marj_durum_yaz(self):
        mi = self.marj_al(self.aktif_sayfa)
        mb = self.baslik_marj_al(self.aktif_sayfa)
        tek_mi = _sayfa_tek_mi(self.aktif_sayfa)
        grup = "TEK" if tek_mi else "CIFT"
        oi = " *ozel" if self.aktif_sayfa in self.marj_sayfa else ""
        ob = " *ozel" if self.aktif_sayfa in self.baslik_marj_sayfa else ""
        self.durum_lbl.config(text=f"[{grup}] Icerik{oi}: u={mi['ust']*100:.0f} a={mi['alt']*100:.0f} s={mi['sol']*100:.0f} g={mi['sag']*100:.0f} | Baslik{ob}: u={mb['ust']*100:.0f} a={mb['alt']*100:.0f}")

    def _marj_ciz(self):
        self.canvas.delete("marj")
        if not self.pdf_doc or self.cozum_modu.get(): return
        r = self.sayfa_rect(); o = self.olcek; cw = r.width * o; ch = r.height * o
        tek_mi = _sayfa_tek_mi(self.aktif_sayfa)

        if self.marj_goster.get():
            m = self.marj_al(self.aktif_sayfa)
            renk = "#FF6D00" if self.aktif_sayfa in self.marj_sayfa else ("#00BCD4" if tek_mi else "#1E88E5")
            self._marj_cerceve_ciz(m, cw, ch, renk, "icerik")

        if self.baslik_marj_goster.get():
            m = self.baslik_marj_al(self.aktif_sayfa)
            renk = "#BF360C" if self.aktif_sayfa in self.baslik_marj_sayfa else ("#E91E63" if tek_mi else "#6A1B9A")
            self._marj_cerceve_ciz(m, cw, ch, renk, "baslik")

    def _marj_cerceve_ciz(self, m, cw, ch, renk, tip):
        x1 = m['sol'] * cw; x2 = m['sag'] * cw; y1 = m['ust'] * ch; y2 = m['alt'] * ch
        self.canvas.create_line(0, y1, cw, y1, fill=renk, width=2, dash=(10, 5), tags="marj")
        self.canvas.create_line(0, y2, cw, y2, fill=renk, width=2, dash=(10, 5), tags="marj")
        self.canvas.create_line(x1, 0, x1, ch, fill=renk, width=2, dash=(10, 5), tags="marj")
        self.canvas.create_line(x2, 0, x2, ch, fill=renk, width=2, dash=(10, 5), tags="marj")
        if tip == 'icerik':
            self.canvas.create_text(6, y1 + 4, text=f"u {m['ust']*100:.0f}%", anchor=tk.NW, fill=renk, font=("Arial", 9, "bold"), tags="marj")
            self.canvas.create_text(6, y2 - 4, text=f"a {m['alt']*100:.0f}%", anchor=tk.SW, fill=renk, font=("Arial", 9, "bold"), tags="marj")
        else:
            self.canvas.create_text(x1 + 4, y1 + 4, text=f"BU {m['ust']*100:.0f}%", anchor=tk.NW, fill=renk, font=("Arial", 9, "bold"), tags="marj")
            self.canvas.create_text(x1 + 4, y2 - 4, text=f"BA {m['alt']*100:.0f}%", anchor=tk.SW, fill=renk, font=("Arial", 9, "bold"), tags="marj")

    def _marj_hedef(self, cx, cy):
        if not self.pdf_doc or self.cozum_modu.get(): return None
        r = self.sayfa_rect(); o = self.olcek; cw = r.width * o; ch = r.height * o
        esik = self.MARJ_TUTMA; adaylar = []
        if self.marj_goster.get():
            m = self.marj_al(self.aktif_sayfa)
            x1 = m['sol'] * cw; x2 = m['sag'] * cw; y1 = m['ust'] * ch; y2 = m['alt'] * ch
            if abs(cx - x1) <= esik and 0 <= cy <= ch: adaylar.append(('icerik', 'sol', abs(cx - x1)))
            if abs(cx - x2) <= esik and 0 <= cy <= ch: adaylar.append(('icerik', 'sag', abs(cx - x2)))
            if abs(cy - y1) <= esik and 0 <= cx <= cw: adaylar.append(('icerik', 'ust', abs(cy - y1)))
            if abs(cy - y2) <= esik and 0 <= cx <= cw: adaylar.append(('icerik', 'alt', abs(cy - y2)))
        if self.baslik_marj_goster.get():
            m = self.baslik_marj_al(self.aktif_sayfa)
            x1 = m['sol'] * cw; x2 = m['sag'] * cw; y1 = m['ust'] * ch; y2 = m['alt'] * ch
            if abs(cx - x1) <= esik and 0 <= cy <= ch: adaylar.append(('baslik', 'sol', abs(cx - x1)))
            if abs(cx - x2) <= esik and 0 <= cy <= ch: adaylar.append(('baslik', 'sag', abs(cx - x2)))
            if abs(cy - y1) <= esik and 0 <= cx <= cw: adaylar.append(('baslik', 'ust', abs(cy - y1)))
            if abs(cy - y2) <= esik and 0 <= cx <= cw: adaylar.append(('baslik', 'alt', abs(cy - y2)))
        if not adaylar: return None
        adaylar.sort(key=lambda t: t[2])
        return (adaylar[0][0], adaylar[0][1])

    def _marj_guncelle(self, tip, anahtar, deger):
        tek_mi = _sayfa_tek_mi(self.aktif_sayfa)
        if tip == 'icerik':
            if self.marj_sayfa_ozel.get():
                m = dict(self.marj_al(self.aktif_sayfa)); m[anahtar] = deger; self.marj_sayfa[self.aktif_sayfa] = m
            else:
                hedef = self.marj_varsayilan_tek if tek_mi else self.marj_varsayilan_cift
                hedef[anahtar] = deger
        else:
            if self.baslik_marj_sayfa_ozel.get():
                m = dict(self.baslik_marj_al(self.aktif_sayfa)); m[anahtar] = deger; self.baslik_marj_sayfa[self.aktif_sayfa] = m
            else:
                hedef = self.baslik_marj_varsayilan_tek if tek_mi else self.baslik_marj_varsayilan_cift
                hedef[anahtar] = deger
            self.sayfa_bilgi = {}

    def bilgi_al(self, p):
        if p not in self.sayfa_bilgi:
            with self.pdf_kilit:
                page = self.pdf_doc.load_page(p)
                marj = self.baslik_marj_al(p)
                self.sayfa_bilgi[p] = sayfa_bilgisi(page, sayfa_kelimeleri(page), baslik_marj=marj)
        return self.sayfa_bilgi[p]

    def listeyi_guncelle(self):
        secili = set(self.test_agaci.selection())
        self.testler = testleri_grupla(self.tum_kutular, self.bilgi_al, self.test_zorla) if self.pdf_doc else []
        self.test_agaci.delete(*self.test_agaci.get_children())
        for i, t in enumerate(self.testler):
            anahtar = t.get('cevap_anahtari', '')
            bilinen = t.get('cevap_bilinen', 0)
            toplam_soru = len([k for _, k in t['sorular'] if not k.get('etiket')])
            goster = anahtar if len(anahtar) <= 20 else anahtar[:20] + "\u2026"
            if toplam_soru: goster = f"{goster}  ({bilinen}/{toplam_soru})"
            self.test_agaci.insert("", "end", iid=str(i), text=t['ad'], values=(t['konu'], t['soru_yazi'], goster, t['sayfa_yazi']), tags=('uyari',) if t['uyari'] else ())
        for iid in secili:
            if self.test_agaci.exists(iid): self.test_agaci.selection_add(iid)
        toplam = sum(len(t['sorular']) for t in self.testler)
        toplam_cevap = sum(t.get('cevap_bilinen', 0) for t in self.testler)
        self.liste_baslik.config(text=f"Testler: {len(self.testler)} | Soru: {toplam} | Bilinen cevap: {toplam_cevap}")
        self._secim_guncelle_ui()

    def _secim_guncelle_ui(self):
        if self.secili is not None and not self.secili_ortak:
            kutular = self.tum_kutular.get(self.aktif_sayfa, [])
            if self.secili < len(kutular):
                k = kutular[self.secili]
                ortak = k.get('ortak_rect')
                if ortak:
                    self.ortak_nums_lbl.config(text=f"Soru {k['q_num']} icin ortak alan var.")
                else:
                    self.ortak_nums_lbl.config(text="Secili sorunun ortak alani yok.")
        elif self.secili is not None and self.secili_ortak:
            self.ortak_nums_lbl.config(text="Ortak alan secili.")
        else:
            self.ortak_nums_lbl.config(text="Secim yok.")

    # ------------------------------------------------------------------
    #  ORTAK ALANA FAREYLE SORU EKLEME MODU
    # ------------------------------------------------------------------
    def _ortak_ekle_aktif(self):
        return self.ortak_aktif is not None and self.ortak_aktif['sayfa'] == self.aktif_sayfa

    def _kol_anahtar(self, bb, W):
        return (0 if bb[0] < 0.45 * W else 1, bb[1])

    def _tahmin_ilk_no(self, rect):
        p = self.aktif_sayfa
        W = self.sayfa_rect().width
        ref = self._kol_anahtar(rect, W)
        kutular = [k for k in self.tum_kutular.get(p, []) if not k.get('etiket')]
        onceki = [k['q_num'] for k in kutular if self._kol_anahtar(k['bbox_pdf'], W) < ref]
        if onceki:
            return max(onceki) + 1
        sonraki = [k['q_num'] for k in kutular]
        for q in range(p - 1, -1, -1):
            ks = [k['q_num'] for k in self.tum_kutular.get(q, []) if not k.get('etiket')]
            if ks:
                tahmin = max(ks) + 1
                return max(1, min(sonraki) - 1) if sonraki and tahmin >= min(sonraki) else tahmin
        return max(1, min(sonraki) - 1) if sonraki and min(sonraki) > 1 else 1

    def ortak_alan_baslat(self, cx=None, cy=None):
        if not self.pdf_doc: return
        self.ortak_ekle_bitir(sessiz=True)
        r = self.sayfa_rect()
        if cx is None:
            cx = self.canvas.canvasx(self.canvas.winfo_width() / 2)
            cy = self.canvas.canvasy(self.canvas.winfo_height() / 3)
        px, py = cx / self.olcek, cy / self.olcek
        w, h = min(300, r.width * 0.8), min(180, r.height * 0.3)
        x0 = min(max(0, px - w / 2), r.width - w); y0 = min(max(0, py - h / 2), r.height - h)
        rect = [round(x0, 2), round(y0, 2), round(x0 + w, 2), round(y0 + h, 2)]
        self.ortak_aktif = {'sayfa': self.aktif_sayfa, 'rect': rect, 'yeni': [], 'mevcut': False}
        self.ilk_no_var.set(self._tahmin_ilk_no(rect))
        self.secili = None; self.secili_ortak = False
        self._ortak_ekle_bilgi()
        self.durum_lbl.config(text="Yeni ortak alan: kenarlarindan boyutlandirin (Ctrl+surukle: tasi), sonra soru alanlarini fareyle cizin / tiklayin. Bitis: Esc.")
        self.kutulari_yeniden_ciz(); self._secim_guncelle_ui()

    def secili_ortaga_soru_ekle_baslat(self):
        kutular = self.tum_kutular.get(self.aktif_sayfa, [])
        if self.secili is None or self.secili >= len(kutular) or not kutular[self.secili].get('ortak_rect'):
            messagebox.showinfo("Bilgi", "Once ortak alani olan bir soru ya da ortak alan secin."); return
        rect = list(kutular[self.secili]['ortak_rect'])
        grup = [q['q_num'] for q in kutular if q.get('ortak_rect') and list(q['ortak_rect']) == rect and not q.get('etiket')]
        self.ortak_ekle_bitir(sessiz=True)
        self.ortak_aktif = {'sayfa': self.aktif_sayfa, 'rect': rect, 'yeni': [], 'mevcut': True}
        self.ilk_no_var.set(max(grup) + 1 if grup else 1)
        self._ortak_ekle_bilgi()
        self.durum_lbl.config(text="Secili ortak alana soru ekleme: soru alanlarini fareyle cizin / tiklayin. Bitis: Esc.")
        self.kutulari_yeniden_ciz()

    def _ortak_ekle_bilgi(self):
        a = self.ortak_aktif
        if a is None:
            self.ortak_ekle_lbl.config(text="Pasif. '+ Yeni Ortak Alan' ile baslayin.", fg="#555"); return
        try: ilk = int(self.ilk_no_var.get())
        except (tk.TclError, ValueError): ilk = 1
        self.ortak_ekle_lbl.config(
            text=f"AKTIF (sayfa {a['sayfa'] + 1}): {len(a['yeni'])} soru eklendi, sonraki no: {ilk + len(a['yeni'])}. "
                 "Bos yere surukleyin = kutu cizer, tek tik = varsayilan kutu. Sag tik: kutuyu sil.", fg="#1B5E20")

    def _ilk_no_degisti(self):
        self._grup_numarala()
        self.kutulari_yeniden_ciz(); self.listeyi_guncelle()

    def _grup_numarala(self):
        a = self.ortak_aktif
        if a is None: return
        try: ilk = int(self.ilk_no_var.get())
        except (tk.TclError, ValueError): return
        W = self.sayfa_rect().width
        for i, k in enumerate(sorted(a['yeni'], key=lambda k: self._kol_anahtar(k['bbox_pdf'], W))):
            k['q_num'] = ilk + i
        self._ortak_ekle_bilgi()

    def _manuel_soru_cevap_ata(self, kutu):
        """Manuel eklenen soru icin cevap anahtarini bulur ve kutu['cevap']'e yazar."""
        if not self.pdf_doc:
            return
        try:
            page = self.pdf_doc.load_page(self.aktif_sayfa)
            kel = sayfa_kelimeleri(page)
            cevaplar = _cevap_meta_tara(kel, [kutu['q_num']])
            if kutu['q_num'] in cevaplar:
                kutu['cevap'] = cevaplar[kutu['q_num']]
        except Exception as e:
            print("manuel cevap atama hatasi:", e)

    def _ortak_soru_ekle(self, bbox):
        a = self.ortak_aktif
        kutular = self.tum_kutular.setdefault(self.aktif_sayfa, [])
        kutu = {'q_num': 0, 'bbox_pdf': [round(v, 2) for v in bbox],
                'text': '(manuel)', 'ortak_rect': list(a['rect'])}
        kutular.append(kutu)
        a['yeni'].append(kutu)
        self._grup_numarala()
        self.secili = len(kutular) - 1
        self.secili_ortak = False
        self._manuel_soru_cevap_ata(kutu)   # cevap ONCE
        self.kutulari_yeniden_ciz()
        self.listeyi_guncelle()             # liste SONRA (cevaplar dahil)
        self._secim_guncelle_ui()

    def ortak_ekle_bitir(self, sessiz=False):
        a = self.ortak_aktif
        if a is None: return
        self.ortak_aktif = None; self.ortak_surukle = None; self.cizim = None
        self.canvas.delete("cizim")
        self._ortak_ekle_bilgi()
        if not sessiz:
            n = len(a['yeni'])
            if n: self.durum_lbl.config(text=f"Ortak alana {n} soru baglandi.")
            elif not a['mevcut']: self.durum_lbl.config(text="Soru eklenmedigi icin yeni ortak alan olusturulmadi.")
        self.kutulari_yeniden_ciz(); self.listeyi_guncelle(); self._secim_guncelle_ui()

    def _kutu_silindi(self, kutu):
        a = self.ortak_aktif
        if a is not None and kutu in a['yeni']:
            a['yeni'].remove(kutu); self._grup_numarala()

    def _kenar_modu(self, rect, cx, cy, tasi=False):
        o = self.olcek
        x1, y1, x2, y2 = [v * o for v in rect]
        m = min(self.TUTMA, max(5, min(x2 - x1, y2 - y1) / 3))
        if not (x1 - m <= cx <= x2 + m and y1 - m <= cy <= y2 + m): return None
        L, R = abs(cx - x1) <= m, abs(cx - x2) <= m
        T, B = abs(cy - y1) <= m, abs(cy - y2) <= m
        if L and R: L = (cx - x1) < (x2 - cx); R = not L
        if T and B: T = (cy - y1) < (y2 - cy); B = not T
        if L or R or T or B: return (L, T, R, B)
        return 'move' if tasi else None

    def _rect_degistir(self, rect0, mod, dx, dy):
        x1, y1, x2, y2 = rect0
        r = self.sayfa_rect(); mw, mh = 10, 8
        if mod == 'move':
            w, h = x2 - x1, y2 - y1
            nx1 = min(max(0, x1 + dx), r.width - w); ny1 = min(max(0, y1 + dy), r.height - h)
            return [round(nx1, 2), round(ny1, 2), round(nx1 + w, 2), round(ny1 + h, 2)]
        L, T, R, B = mod
        nx1, ny1, nx2, ny2 = x1, y1, x2, y2
        if L: nx1 = min(max(0, x1 + dx), x2 - mw)
        if R: nx2 = max(min(r.width, x2 + dx), x1 + mw)
        if T: ny1 = min(max(0, y1 + dy), y2 - mh)
        if B: ny2 = max(min(r.height, y2 + dy), y1 + mh)
        return [round(nx1, 2), round(ny1, 2), round(nx2, 2), round(ny2, 2)]

    def _ortak_surukle_hareket(self, event):
        d = self.ortak_surukle; a = self.ortak_aktif
        if d is None or a is None: return
        px, py = self.canvas.canvasx(event.x) / self.olcek, self.canvas.canvasy(event.y) / self.olcek
        yeni = self._rect_degistir(d['rect0'], d['mod'], px - d['bas'][0], py - d['bas'][1])
        eski = list(a['rect']); a['rect'] = yeni
        for k in self.tum_kutular.get(a['sayfa'], []):
            if k.get('ortak_rect') and list(k['ortak_rect']) == eski:
                k['ortak_rect'] = list(yeni)
        self.kutulari_yeniden_ciz()

    def _cizim_surukle(self, event):
        c = self.cizim
        cx, cy = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
        if not c['surukle'] and abs(cx - c['x0']) < 5 and abs(cy - c['y0']) < 5: return
        c['surukle'] = True
        if c['id'] is None:
            c['id'] = self.canvas.create_rectangle(c['x0'], c['y0'], cx, cy, outline="#00E5FF", width=2, dash=(3, 2), tags="cizim")
        else:
            self.canvas.coords(c['id'], c['x0'], c['y0'], cx, cy)

    def _cizim_bitir(self, event):
        c = self.cizim; self.cizim = None
        self.canvas.delete("cizim")
        if self.ortak_aktif is None or c is None: return
        o = self.olcek; r = self.sayfa_rect()
        cx, cy = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
        bbox = None
        if c['surukle']:
            x1, x2 = sorted((c['x0'], cx)); y1, y2 = sorted((c['y0'], cy))
            if (x2 - x1) >= 12 and (y2 - y1) >= 10:
                bbox = [max(0, x1 / o), max(0, y1 / o), min(r.width, x2 / o), min(r.height, y2 / o)]
        if bbox is None:
            px, py = c['x0'] / o, c['y0'] / o
            x0, y0 = max(0, px - 6), max(0, py - 6)
            x_son = r.width * 0.98 if px >= r.width / 2 else r.width / 2 - 3
            x_son = min(r.width, max(x_son, x0 + 80))
            bbox = [x0, y0, x_son, min(r.height, y0 + 130)]
        self._ortak_soru_ekle(bbox)

    def yeni_kutu_ekle(self, ortak=False, tiklanan_pos=None):
        if not self.pdf_doc: 
            return
        if ortak:
            self.ortak_alan_baslat()
            return

        r = self.sayfa_rect()
        kutular = self.tum_kutular.setdefault(self.aktif_sayfa, [])
        mevcut_nolar = sorted([k['q_num'] for k in kutular if not k.get('etiket')])
        varsayilan_no = (mevcut_nolar[-1] + 1) if mevcut_nolar else 1

        # Kullanıcıdan soru numarasını belirlemesini iste
        from tkinter import simpledialog
        hedef_no = simpledialog.askinteger(
            "Soru Numarası", 
            f"Eklenecek soru numarasını girin:\n(Mevcut bir numara girerseniz sonrakiler otomatik ötelenir)",
            initialvalue=varsayilan_no, minvalue=1, maxvalue=999, parent=self.root
        )
        if hedef_no is None:
            return

        # Araya ekleme yapılıyorsa hedef_no ve sonrasındaki soruları 1 artır
        if hedef_no in mevcut_nolar:
            for k in kutular:
                if not k.get('etiket') and k['q_num'] >= hedef_no:
                    k['q_num'] += 1
                    if k.get('text', '').endswith('.'):
                        k['text'] = f"{k['q_num']}."

        # Konum belirleme: Tıklanan konum varsa oraya, yoksa önceki/sonraki soruya göre veya ortaya yerleştir
        if tiklanan_pos:
            px, py = tiklanan_pos
            w, h = 200, 100
            x0 = max(0, px - w / 2)
            y0 = max(0, py - h / 2)
        else:
            # Sayfadaki bir önceki sorunun altına yerleştirmeyi dene
            onceki_soru = next((k for k in kutular if k['q_num'] == hedef_no - 1), None)
            if onceki_soru:
                bx = onceki_soru['bbox_pdf']
                x0 = bx[0]
                y0 = min(r.height - 110, bx[3] + 15)
                w = bx[2] - bx[0]
                h = min(120, max(60, bx[3] - bx[1]))
            else:
                x0, y0 = r.width * 0.1, r.height * 0.2
                w, h = r.width * 0.38, 120

        x1 = min(r.width, x0 + w)
        y1 = min(r.height, y0 + h)

        kutu = {
            'q_num': hedef_no,
            'bbox_pdf': [round(x0, 2), round(y0, 2), round(x1, 2), round(y1, 2)],
            'text': f"{hedef_no}."
        }
        kutular.append(kutu)

        # Kutuları q_num sırasına göre sıralı tut
        kutular.sort(key=lambda k: (0 if not k.get('etiket') else 1, k.get('q_num', 0)))

        self.secili = kutular.index(kutu)
        self.secili_ortak = False
        self._manuel_soru_cevap_ata(kutu)
        self.kutulari_yeniden_ciz()
        self.listeyi_guncelle()
        self._secim_guncelle_ui()

    def ortak_alan_duzenle_dialog(self):
        if self.secili is None or not self.secili_ortak:
            messagebox.showinfo("Bilgi", "Lutfen once bir ortak alani secin.")
            return
        kutular = self.tum_kutular.get(self.aktif_sayfa, [])
        if self.secili >= len(kutular): return
        k = kutular[self.secili]
        rect = k.get('ortak_rect')
        if not rect: return

        nums = [str(k2['q_num']) for k2 in kutular if k2.get('ortak_rect') is not None and list(k2['ortak_rect']) == list(rect)]

        def cb(new_nums, new_rect):
            for k2 in kutular:
                if k2['q_num'] in new_nums:
                    k2['ortak_rect'] = list(new_rect)
                elif k2.get('ortak_rect') is not None and list(k2['ortak_rect']) == list(rect):
                    del k2['ortak_rect']
            self.kutulari_yeniden_ciz()
            self._secim_guncelle_ui()

        OrtakAlanDuzenle(self.root, ", ".join(nums), rect, cb)

    def akilli_tahta_ac(self):
        """Ogrenci / ogretmen PDF farklarini bulup cevaplari tiklayinca gosteren pencere."""
        pen = getattr(self, '_tahta_pencere', None)
        try:
            if pen is not None and pen.winfo_exists():
                pen.deiconify(); pen.lift(); pen.focus_force(); return
        except tk.TclError:
            pass
        self._tahta_pencere = AkilliTahtaPenceresi(self)

    def sayfa_tani(self):
        """Aktif sayfada soru numarasi adaylari, yonergeler ve elenen sorular icin ayrintili rapor."""
        if not self.pdf_doc: return
        i = self.aktif_sayfa
        tek_mi = _sayfa_tek_mi(i)
        ic_marj = self.marj_sayfa.get(i, self.marj_varsayilan_tek if tek_mi else self.marj_varsayilan_cift)
        tani = []
        try:
            with self.pdf_kilit:
                page = self.pdf_doc.load_page(i)
                kel = sayfa_kelimeleri(page)
                sonuc = sorulari_bul(page, 1.0, self.secenekli_sart.get(), self.etkinlik_bul.get(), kel, marj=ic_marj, tani=tani)
            tani.append("")
            for q in sorted(sonuc, key=lambda q: (q['kol'], q['bbox_pdf'][1])):
                tani.append(f"SONUC soru {q['q_num']} kol={q['kol']} bbox={[round(v) for v in q['bbox_pdf']]} ortak={[round(v) for v in q['ortak_rect']] if q.get('ortak_rect') else None}")
        except Exception as e:
            import traceback; tani.append(traceback.format_exc())
        pen = tk.Toplevel(self.root); pen.title(f"Sayfa {i + 1} tani raporu"); pen.geometry("900x600")
        t = tk.Text(pen, wrap="none", font=("Consolas", 10)); t.pack(fill=tk.BOTH, expand=True)
        t.insert("1.0", "\n".join(tani))

    def sayfa_test_zorla(self, deger):
        if not self.pdf_doc: return
        if deger is None: self.test_zorla.pop(self.aktif_sayfa, None)
        else:
            if not self.tum_kutular.get(self.aktif_sayfa):
                messagebox.showinfo("Bilgi", "Bu sayfada kutu yok.")
                return
            self.test_zorla[self.aktif_sayfa] = deger
        self.listeyi_guncelle()

    def teste_git(self, event=None):
        sec = self.test_agaci.selection()
        if not sec: return
        t = self.testler[int(sec[0])]
        self.sayfa_degistir(t['sayfalar'][0])

    def cevap_anahtari_goster(self):
        if not self.testler:
            messagebox.showinfo("Bilgi", "Once PDF tarayin."); return
        win = tk.Toplevel(self.root); win.title("Cevap Anahtarlari"); win.geometry("820x640"); win.configure(bg="#eceff1")
        govde = tk.Frame(win, bg="#eceff1"); govde.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        txt = tk.Text(govde, font=("Courier New", 11), wrap=tk.NONE, bg="white")
        sb = tk.Scrollbar(govde, orient=tk.VERTICAL, command=txt.yview)
        txt.configure(yscrollcommand=sb.set); sb.pack(side=tk.RIGHT, fill=tk.Y); txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        parcalar = []
        for t in self.testler:
            soru_kel = [k for _, k in t['sorular'] if not k.get('etiket')]
            if not soru_kel: continue
            cevaplar = [k.get('cevap', '') or '?' for k in soru_kel]
            bas = f"=== {t['ad']} ({t['konu']}) [{t['sayfa_yazi']}] ===\n"
            no_satir = "  Soru : " + " ".join(f"{k['q_num']:>3}" for k in soru_kel) + "\n"
            cv_satir = "  Cevap: " + " ".join(f"{c:>3}" for c in cevaplar) + "\n"
            ak_satir = "  Aktr : " + "".join(c for c in cevaplar) + "\n"
            parcalar.append(bas + no_satir + cv_satir + ak_satir)
        txt.insert("1.0", "\n".join(parcalar))
        txt.config(state=tk.DISABLED)

    def pdf_uret(self, hepsi):
        if not self.pdf_doc or not self.testler:
            messagebox.showwarning("Uyari", "Once bir PDF acip taramalisiniz."); return
        secilen = list(self.testler) if hepsi else [self.testler[int(i)] for i in sorted([int(i) for i in self.test_agaci.selection()])]
        if not secilen: return
        klasor = filedialog.askdirectory(title="PDF dosyalarinin kaydedilecegi klasoru secin")
        if not klasor: return
        isler = []
        for t in secilen:
            sorular = [(p, list(k['bbox_pdf']), bool(k.get('etiket')), list(k['ortak_rect']) if k.get('ortak_rect') else None) for p, k in t['sorular']]
            temel = dosya_adi_temizle(f"{t['ad']} - {t['konu']}" if t['konu'] else t['ad'])[:90]
            cevaplar = "".join((k.get('cevap') or '-') for _, k in t['sorular'] if not k.get('etiket'))
            ad = temel + (f"_{cevaplar}" if cevaplar.strip('-') else "") + ".pdf"
            isler.append((ad, sorular))
        self.pdf_btn_secili.config(state=tk.DISABLED); self.pdf_btn_hepsi.config(state=tk.DISABLED); self.ilerme["value"] = 0
        threading.Thread(target=self._pdf_uret_is, args=(klasor, isler, self.raster_pdf.get(), self.cevap_gizle.get(), self.cevap_satiri_sil.get()), daemon=True).start()

    def _pdf_uret_is(self, klasor, isler, raster, cevap_gizle=True, cevap_satiri_sil=True):
        yazilan, hatalar, onbellek = [], [], {}
        for n, (ad, sorular) in enumerate(isler, 1):
            self._ui(lambda ad=ad, n=n: self.durum_lbl.config(text=f"PDF uretiliyor ({n}/{len(isler)}): {ad}"))
            yol = os.path.join(klasor, ad)
            try:
                sayfa = test_pdf_yaz(self.pdf_doc, self.pdf_kilit, sorular, yol, raster, onbellek, cevap_gizle=cevap_gizle, cevap_satiri_sil=cevap_satiri_sil)
                yazilan.append((ad, sayfa))
            except Exception as e:
                hatalar.append(f"{ad}: {e}")
            self._ui(lambda v=(n / len(isler)) * 100: self.ilerme.config(value=v))
        self._ui(lambda: self._pdf_uret_bitti(klasor, yazilan, hatalar))

    def _pdf_uret_bitti(self, klasor, yazilan, hatalar):
        self.pdf_btn_secili.config(state=tk.NORMAL); self.pdf_btn_hepsi.config(state=tk.NORMAL)
        self.durum_lbl.config(text=f"{len(yazilan)} PDF kaydedildi: {klasor}")
        mesaj = f"{len(yazilan)} test PDF'i kaydedildi:\n{klasor}\n\nToplam sayfa (soru): {sum(s for _, s in yazilan)}"
        if hatalar: mesaj += "\n\nHatalar:\n" + "\n".join(hatalar); messagebox.showwarning("Tamamlandi", mesaj)
        else: messagebox.showinfo("Tamamlandi", mesaj)

    def pdf_ac(self):
        dosya = filedialog.askopenfilename(filetypes=[("PDF Dosyalari", "*.pdf")])
        if not dosya: return
        with self.pdf_kilit:
            self.pdf_yolu = dosya
            if self.pdf_doc is not None:
                try: self.pdf_doc.close()
                except: pass
            self.pdf_doc = fitz.open(dosya)
            self.toplam_sayfa = len(self.pdf_doc)
        self.aktif_sayfa = 0; self.ortak_aktif = None
        self.tum_kutular = {}; self.sayfa_bilgi = {}; self.test_zorla = {}
        self.marj_varsayilan_tek = dict(MARJ_VARSAYILAN_TEK); self.marj_varsayilan_cift = dict(MARJ_VARSAYILAN_CIFT); self.marj_sayfa = {}
        self.baslik_marj_varsayilan_tek = dict(MARJ_BASLIK_VARSAYILAN_TEK); self.baslik_marj_varsayilan_cift = dict(MARJ_BASLIK_VARSAYILAN_CIFT); self.baslik_marj_sayfa = {}
        self.secili = None; self.secili_ortak = False
        self.listeyi_guncelle()
        self.durum_lbl.config(text=f"PDF yuklendi: {os.path.basename(dosya)} | {self.toplam_sayfa} sayfa")
        self.root.update_idletasks(); self.sigdir(); self._marj_ciz()

        otomatik = os.path.join(os.path.dirname(dosya), "soru_kutulari.json")
        if os.path.isfile(otomatik):
            try:
                with open(otomatik, 'r', encoding='utf-8') as f:
                    kayit = json.load(f)
                if (os.path.abspath(kayit.get('pdf_yolu', '')) == os.path.abspath(dosya)
                        and messagebox.askyesno(
                            "Kayit bulundu",
                            "Bu PDF icin kayitli kutular var.\n\nYuklensin mi?")):
                    self.kutulari_yukle_json(otomatik)
            except Exception as e:
                print(f"otomatik kayit okunamadi: {e}")

    def sayfa_rect(self):
        with self.pdf_kilit: return self.pdf_doc.load_page(self.aktif_sayfa).rect

    def sayfayi_ciz(self):
        if not self.pdf_doc: return
        self.sayfa_lbl.config(text=f"Sayfa: {self.aktif_sayfa + 1} / {self.toplam_sayfa}")
        with self.pdf_kilit:
            page = self.pdf_doc.load_page(self.aktif_sayfa)
            pix = page.get_pixmap(matrix=fitz.Matrix(self.olcek, self.olcek), alpha=False)
        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        self.img_tk = ImageTk.PhotoImage(img)
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor=tk.NW, image=self.img_tk)
        self.canvas.config(scrollregion=(0, 0, pix.width, pix.height))
        self.zoom_lbl.config(text=f"{int(round(self.olcek * 100))}%")
        self.kutulari_yeniden_ciz(); self._marj_ciz()

    def sigdir(self):
        if not self.pdf_doc: return
        self.sigdir_modu = True
        cw, ch = self.canvas.winfo_width(), self.canvas.winfo_height()
        if cw < 50 or ch < 50: return
        r = self.sayfa_rect()
        self.olcek = max(self.MIN_ZOOM, min(self.MAX_ZOOM, (cw - 4) / r.width, (ch - 4) / r.height))
        self.sayfayi_ciz(); self.canvas.xview_moveto(0); self.canvas.yview_moveto(0)

    def pencere_boyutu_degisti(self, event):
        if not (self.sigdir_modu and self.pdf_doc): return
        if self._fit_job: self.root.after_cancel(self._fit_job)
        self._fit_job = self.root.after(120, self.sigdir)

    def yakinlastir(self, carpan, olay=None):
        if not self.pdf_doc: return
        eski = self.olcek; yeni = max(self.MIN_ZOOM, min(self.MAX_ZOOM, eski * carpan))
        if abs(yeni - eski) < 1e-6: return
        self.sigdir_modu = False
        ex, ey = (olay.x, olay.y) if olay else (self.canvas.winfo_width() / 2, self.canvas.winfo_height() / 2)
        px = self.canvas.canvasx(ex) / eski; py = self.canvas.canvasy(ey) / eski
        self.olcek = yeni; self.sayfayi_ciz(); r = self.sayfa_rect()
        self.canvas.xview_moveto(max(0.0, (px * yeni - ex) / (r.width * yeni))); self.canvas.yview_moveto(max(0.0, (py * yeni - ey) / (r.height * yeni)))

    def tekerlek(self, event, yon=None):
        if yon is None:
            yon = 1 if event.delta > 0 else -1
        self.canvas.yview_scroll(-3 * yon, "units")

    def tekerlek_yatay(self, event):
        self.canvas.xview_scroll(-3 if event.delta > 0 else 3, "units")

    def tekerlek_zoom(self, event, yon=None):
        if yon is None:
            yon = 1 if event.delta > 0 else -1
        self.yakinlastir(1.15 if yon > 0 else 1 / 1.15, event)

    def sayfa_degistir(self, yeni):
        self.ortak_ekle_bitir(sessiz=True)
        self.aktif_sayfa = yeni; self.secili = None; self.secili_ortak = False
        if self.sigdir_modu: self.sigdir()
        else: self.sayfayi_ciz()
        self._secim_guncelle_ui()

    def sonraki_sayfa(self):
        if self.pdf_doc and self.aktif_sayfa < self.toplam_sayfa - 1: self.sayfa_degistir(self.aktif_sayfa + 1)
    def onceki_sayfa(self):
        if self.pdf_doc and self.aktif_sayfa > 0: self.sayfa_degistir(self.aktif_sayfa - 1)

    def tum_sayfalari_tara_threaded(self):
        if not self.pdf_doc: return
        self.tara_btn.config(state=tk.DISABLED); self.ilerme["value"] = 0
        threading.Thread(target=self.tum_sayfalari_tara, args=(self.secenekli_sart.get(), self.etkinlik_bul.get()), daemon=True).start()

    def tum_sayfalari_tara(self, sart, etk):
        toplam = self.toplam_sayfa; yeni, yeni_bilgi = {}, {}
        for i in range(toplam):
            self._ui(lambda i=i: self.durum_lbl.config(text=f"Sayfa {i + 1}/{toplam} taraniyor..."))
            tek_mi = _sayfa_tek_mi(i)
            ic_marj = self.marj_sayfa.get(i, self.marj_varsayilan_tek if tek_mi else self.marj_varsayilan_cift)
            bs_marj = self.baslik_marj_sayfa.get(i, self.baslik_marj_varsayilan_tek if tek_mi else self.baslik_marj_varsayilan_cift)
            try:
                with self.pdf_kilit:
                    page = self.pdf_doc.load_page(i)
                    kel = sayfa_kelimeleri(page)
                    yeni_bilgi[i] = sayfa_bilgisi(page, kel, baslik_marj=bs_marj)
                    bulunanlar = sorulari_bul(page, 1.0, sart, etk, kel, marj=ic_marj)
            except Exception as e:
                bulunanlar = []
            yeni[i] = []
            for q in bulunanlar:
                kayit = {'q_num': q['q_num'], 'bbox_pdf': list(q['bbox_pdf']), 'text': q['text']}
                if q.get('etiket'): kayit['etiket'] = q['etiket']
                if q.get('cevap'): kayit['cevap'] = q['cevap']
                if q.get('ortak_rect'): kayit['ortak_rect'] = list(q['ortak_rect'])
                yeni[i].append(kayit)
            self._ui(lambda v=((i + 1) / toplam) * 100: self.ilerme.config(value=v))
        self._ui(lambda: self.tarama_bitti(yeni, yeni_bilgi))

    def tarama_bitti(self, yeni, yeni_bilgi):
        self.ortak_aktif = None; self.tum_kutular = yeni; self.sayfa_bilgi.update(yeni_bilgi)
        self.tara_btn.config(state=tk.NORMAL); self.secili = None; self.secili_ortak = False
        self.listeyi_guncelle()
        self.durum_lbl.config(text=f"Tarama tamamlandi! {len(self.testler)} test.")
        self.kutulari_yeniden_ciz(); self._marj_ciz()

    def kutulari_yeniden_ciz(self):
        self.canvas.delete("kutu")
        o = self.olcek
        kutular = self.tum_kutular.get(self.aktif_sayfa, [])
        cozum = self.cozum_modu.get()

        for idx, k in enumerate(kutular):
            if k.get('ortak_rect'):
                ox1, oy1, ox2, oy2 = [v * o for v in k['ortak_rect']]
                ortak_renk = "#00e676" if (self.secili == idx and self.secili_ortak) else "#FFD54F"
                self.canvas.create_rectangle(ox1, oy1, ox2, oy2, outline=ortak_renk, width=2, dash=(4,2), tags="kutu")
                self.canvas.create_text(ox1 + 3, oy1 - 2, text="Ortak", anchor=tk.SW, fill=ortak_renk, font=("Arial", 9, "bold"), tags="kutu")

            x1, y1, x2, y2 = [v * o for v in k['bbox_pdf']]
            if cozum:
                self._mercek_ciz(x2 - 18, y1 + 18, idx)
            else:
                soru_renk = "lime" if (self.secili == idx and not self.secili_ortak) else "red"
                self.canvas.create_rectangle(x1, y1, x2, y2, outline=soru_renk, width=2, tags="kutu")
                etiket = k.get('etiket') or f"{k['q_num']}."
                cv = k.get('cevap')
                if cv: etiket = f"{etiket} [{cv}]"
                self.canvas.create_text(x1 + 3, max(10, y1 - 2), text=etiket, anchor=tk.SW, fill="yellow", font=("Arial", 11, "bold"), tags="kutu")

            if not cozum and self.secili == idx:
                hedef_x1, hedef_y1, hedef_x2, hedef_y2 = x1, y1, x2, y2
                if self.secili_ortak and k.get('ortak_rect'):
                    hedef_x1, hedef_y1, hedef_x2, hedef_y2 = [v * o for v in k['ortak_rect']]
                xm, ym = (hedef_x1 + hedef_x2) / 2, (hedef_y1 + hedef_y2) / 2
                for hx, hy, r in [(hedef_x1, hedef_y1, 7), (hedef_x2, hedef_y1, 7), (hedef_x1, hedef_y2, 7), (hedef_x2, hedef_y2, 7), (xm, hedef_y1, 6), (xm, hedef_y2, 6), (hedef_x1, ym, 6), (hedef_x2, ym, 6)]:
                    self.canvas.create_rectangle(hx - r, hy - r, hx + r, hy + r, fill="#00e676", outline="black", width=1, tags="kutu")
        self._aktif_ortak_ciz()

    def _aktif_ortak_ciz(self):
        a = self.ortak_aktif
        if not a or a['sayfa'] != self.aktif_sayfa: return
        o = self.olcek
        x1, y1, x2, y2 = [v * o for v in a['rect']]
        self.canvas.create_rectangle(x1, y1, x2, y2, outline="#00E5FF", width=3, dash=(6, 3), tags="kutu")
        self.canvas.create_text(x1 + 3, max(10, y1 - 2), text="ORTAK ALAN (aktif)", anchor=tk.SW, fill="#00E5FF", font=("Arial", 10, "bold"), tags="kutu")
        xm, ym = (x1 + x2) / 2, (y1 + y2) / 2
        for hx, hy in [(x1, y1), (x2, y1), (x1, y2), (x2, y2), (xm, y1), (xm, y2), (x1, ym), (x2, ym)]:
            self.canvas.create_rectangle(hx - 6, hy - 6, hx + 6, hy + 6, fill="#00E5FF", outline="black", tags="kutu")

    def _vurgu(self, cx, cy, yalniz_kenar=False):
        kutular = self.tum_kutular.get(self.aktif_sayfa, [])
        o = self.olcek

        for i in range(len(kutular) - 1, -1, -1):
            x1, y1, x2, y2 = [v * o for v in kutular[i]['bbox_pdf']]
            m = min(self.TUTMA, max(5, min(x2 - x1, y2 - y1) / 3))
            if not (x1 - m <= cx <= x2 + m and y1 - m <= cy <= y2 + m): continue
            L, R = abs(cx - x1) <= m, abs(cx - x2) <= m
            T, B = abs(cy - y1) <= m, abs(cy - y2) <= m
            if L and R: L = (cx - x1) < (x2 - cx); R = not L
            if T and B: T = (cy - y1) < (y2 - cy); B = not T
            if L or R or T or B: return i, (L, T, R, B), False
            return i, 'move', False

        for i in range(len(kutular) - 1, -1, -1):
            k = kutular[i]
            if not k.get('ortak_rect'): continue
            x1, y1, x2, y2 = [v * o for v in k['ortak_rect']]
            m = min(self.TUTMA, max(5, min(x2 - x1, y2 - y1) / 3))
            if not (x1 - m <= cx <= x2 + m and y1 - m <= cy <= y2 + m): continue
            L, R = abs(cx - x1) <= m, abs(cx - x2) <= m
            T, B = abs(cy - y1) <= m, abs(cy - y2) <= m
            if L and R: L = (cx - x1) < (x2 - cx); R = not L
            if T and B: T = (cy - y1) < (y2 - cy); B = not T
            if L or R or T or B: return i, (L, T, R, B), True
            if yalniz_kenar: continue
            return i, 'move', True

        return None, None, False

    def _imlec_ayarla(self, mod):
        try:
            if mod is None:
                self.canvas.config(cursor="crosshair")
                return
            if mod == 'move':
                self.canvas.config(cursor="fleur")
                return
            L, T, R, B = mod
            win = sys.platform.startswith("win")
            if (L or R) and (T or B):
                nw_se = (L and T) or (R and B)
                if win:
                    self.canvas.config(cursor="size_nw_se" if nw_se else "size_ne_sw")
                else:
                    self.canvas.config(cursor="crosshair")
            elif L or R:
                self.canvas.config(cursor="size_we" if win else "sb_h_double_arrow")
            else:
                self.canvas.config(cursor="size_ns" if win else "sb_v_double_arrow")
        except tk.TclError:
            self.canvas.config(cursor="crosshair")

    def fare_gez(self, event):
        if self.surukleme_modu is not None or self.marj_surukle is not None or not self.pdf_doc: return
        cx, cy = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
        hedef = self._marj_hedef(cx, cy)
        if hedef:
            _, anahtar = hedef
            self.canvas.config(cursor="sb_h_double_arrow" if anahtar in ('sol', 'sag') else "sb_v_double_arrow")
            return
        if self.cozum_modu.get(): return
        if self._ortak_ekle_aktif():
            m = self._kenar_modu(self.ortak_aktif['rect'], cx, cy)
            if m: self._imlec_ayarla(m); return
        _, mod, _ = self._vurgu(cx, cy, yalniz_kenar=self._ortak_ekle_aktif())
        self._imlec_ayarla(mod)

    def fare_tikla(self, event):
        if not self.pdf_doc: return
        cx, cy = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
        hedef = self._marj_hedef(cx, cy)
        if hedef: self.marj_surukle = hedef; return
        if self.cozum_modu.get():
            items = self.canvas.find_overlapping(event.x, event.y, event.x, event.y)
            for it in reversed(items):
                for t in self.canvas.gettags(it):
                    if t.startswith("mercek_"): self._cozum_ac(int(t.split("_", 1)[1])); return
            return

        mod_aktif = self._ortak_ekle_aktif()
        if mod_aktif:
            m = self._kenar_modu(self.ortak_aktif['rect'], cx, cy, tasi=bool(event.state & 0x4))
            if m:
                self.ortak_surukle = {'mod': m, 'bas': (cx / self.olcek, cy / self.olcek), 'rect0': list(self.ortak_aktif['rect'])}
                self.surukleme_modu = None; self._imlec_ayarla(m); return

        idx, mod, ortak = self._vurgu(cx, cy, yalniz_kenar=mod_aktif)
        self.secili = idx
        self.secili_ortak = ortak

        if idx is None and mod_aktif:
            self.cizim = {'x0': cx, 'y0': cy, 'id': None, 'surukle': False}
            self.surukleme_modu = None; self.kutulari_yeniden_ciz(); self._secim_guncelle_ui(); return

        if idx is None:
            self.surukleme_modu = None; self.kutulari_yeniden_ciz(); self._secim_guncelle_ui(); return

        if ortak:
            self.kutu_baslangic = list(self.tum_kutular[self.aktif_sayfa][idx]['ortak_rect'])
        else:
            self.kutu_baslangic = list(self.tum_kutular[self.aktif_sayfa][idx]['bbox_pdf'])

        self.surukleme_baslangic = (cx / self.olcek, cy / self.olcek)
        self.surukleme_modu = mod
        self._imlec_ayarla(mod)
        self.kutulari_yeniden_ciz()
        self._secim_guncelle_ui()

    def fare_surukle(self, event):
        if self.marj_surukle:
            tip, anahtar = self.marj_surukle
            cx, cy = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
            r = self.sayfa_rect(); o = self.olcek; cw = r.width * o; ch = r.height * o
            if cw <= 0 or ch <= 0: return
            fx = min(max(0.0, cx / cw), 1.0); fy = min(max(0.0, cy / ch), 1.0)
            mevcut = self.marj_al(self.aktif_sayfa) if tip == 'icerik' else self.baslik_marj_al(self.aktif_sayfa)
            if anahtar == 'sol': self._marj_guncelle(tip, 'sol', min(fx, mevcut['sag'] - 0.05))
            elif anahtar == 'sag': self._marj_guncelle(tip, 'sag', max(fx, mevcut['sol'] + 0.05))
            elif anahtar == 'ust': self._marj_guncelle(tip, 'ust', min(fy, mevcut['alt'] - 0.05))
            elif anahtar == 'alt': self._marj_guncelle(tip, 'alt', max(fy, mevcut['ust'] + 0.05))
            self._marj_ciz(); self._marj_durum_yaz(); return

        if self.ortak_surukle is not None: self._ortak_surukle_hareket(event); return
        if self.cizim is not None: self._cizim_surukle(event); return
        if self.cozum_modu.get() or self.secili is None or self.kutu_baslangic is None or self.surukleme_modu is None: return
        px, py = self.canvas.canvasx(event.x) / self.olcek, self.canvas.canvasy(event.y) / self.olcek
        dx, dy = px - self.surukleme_baslangic[0], py - self.surukleme_baslangic[1]
        x1, y1, x2, y2 = self.kutu_baslangic
        r = self.sayfa_rect(); mw, mh = 10, 8

        if self.surukleme_modu == 'move':
            w, h = x2 - x1, y2 - y1
            nx1 = min(max(0, x1 + dx), r.width - w); ny1 = min(max(0, y1 + dy), r.height - h)
            nx2, ny2 = nx1 + w, ny1 + h
        else:
            L, T, R, B = self.surukleme_modu
            nx1, ny1, nx2, ny2 = x1, y1, x2, y2
            if L: nx1 = min(max(0, x1 + dx), x2 - mw)
            if R: nx2 = max(min(r.width, x2 + dx), x1 + mw)
            if T: ny1 = min(max(0, y1 + dy), y2 - mh)
            if B: ny2 = max(min(r.height, y2 + dy), y1 + mh)

        yeni_rect = [round(nx1, 2), round(ny1, 2), round(nx2, 2), round(ny2, 2)]

        if self.secili_ortak:
            eski_rect = self.tum_kutular[self.aktif_sayfa][self.secili].get('ortak_rect')
            self.tum_kutular[self.aktif_sayfa][self.secili]['ortak_rect'] = yeni_rect
            for k in self.tum_kutular[self.aktif_sayfa]:
                if k.get('ortak_rect') == eski_rect and k is not self.tum_kutular[self.aktif_sayfa][self.secili]:
                    k['ortak_rect'] = yeni_rect
        else:
            self.tum_kutular[self.aktif_sayfa][self.secili]['bbox_pdf'] = yeni_rect

        self.kutulari_yeniden_ciz()

    def fare_birak(self, event):
        if self.marj_surukle: self.marj_surukle = None; self._marj_durum_yaz(); return
        if self.ortak_surukle is not None: self.ortak_surukle = None; self.listeyi_guncelle(); self.fare_gez(event); return
        if self.cizim is not None: self._cizim_bitir(event); self.fare_gez(event); return
        self.surukleme_modu = None; self.kutu_baslangic = None
        if self._ortak_ekle_aktif(): self._grup_numarala(); self.kutulari_yeniden_ciz(); self.listeyi_guncelle()
        self.fare_gez(event)

    def sag_tikla(self, event):
        if not self.pdf_doc or self.cozum_modu.get(): return
        i, _, _ = self._vurgu(self.canvas.canvasx(event.x), self.canvas.canvasy(event.y))
        if i is not None:
            silinen = self.tum_kutular[self.aktif_sayfa].pop(i)
            self._kutu_silindi(silinen)
            self.secili = None; self.secili_ortak = False
            self.kutulari_yeniden_ciz(); self.listeyi_guncelle()

    def cift_tikla(self, event):
        if not self.pdf_doc or self.cozum_modu.get(): return
        cx, cy = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
        i, _, _ = self._vurgu(cx, cy, yalniz_kenar=self._ortak_ekle_aktif())
        if i is not None or self._ortak_ekle_aktif(): return
        if self.ortak_mod.get(): 
            self.ortak_alan_baslat(cx, cy)
        else: 
            self.yeni_kutu_ekle(False, tiklanan_pos=(cx / self.olcek, cy / self.olcek))

    def secili_sil(self, event=None):
        if self.cozum_modu.get() or self.secili is None: return
        kutular = self.tum_kutular.get(self.aktif_sayfa, [])
        if 0 <= self.secili < len(kutular):
            self._kutu_silindi(kutular.pop(self.secili))
            self.secili = None; self.secili_ortak = False
            self.kutulari_yeniden_ciz(); self.listeyi_guncelle()

    def her_seyi_temizle(self):
        if not messagebox.askyesno("Onay", "Tum kutular silinsin mi?"): return
        self.ortak_aktif = None; self.tum_kutular = {}; self.secili = None; self.secili_ortak = False
        self.kutulari_yeniden_ciz(); self.listeyi_guncelle(); self.durum_lbl.config(text="Tum kutular temizlendi.")

    def _cozum_modu_degisti(self):
        self.secili = None; self.secili_ortak = False
        self.canvas.config(cursor="hand2" if self.cozum_modu.get() else "crosshair")
        self.kutulari_yeniden_ciz(); self._marj_ciz()

    def _ortak_mod_degisti(self):
        if self.ortak_mod.get(): self.cozum_modu.set(False); self._cozum_modu_degisti()
        self.canvas.config(cursor="crosshair")
        self.kutulari_yeniden_ciz()

    def _mercek_ciz(self, cx, cy, idx, r=13):
        tag = f"mercek_{idx}"
        self.canvas.create_oval(cx - r, cy - r, cx + r, cy + r, outline="#0d47a1", width=2, fill="#E3F2FD", tags=("kutu", tag))
        self.canvas.create_line(cx + r * 0.55, cy + r * 0.55, cx + r * 1.55, cy + r * 1.55, fill="#0d47a1", width=4, capstyle=tk.ROUND, tags=("kutu", tag))
        self.canvas.create_line(cx - 5, cy, cx + 5, cy, fill="#0d47a1", width=2, tags=("kutu", tag))
        self.canvas.create_line(cx, cy - 5, cx, cy + 5, fill="#0d47a1", width=2, tags=("kutu", tag))

    def _cozum_ac(self, idx):
        kutular = self.tum_kutular.get(self.aktif_sayfa, [])
        if idx < 0 or idx >= len(kutular): return
        k = kutular[idx]
        etiket = k.get('etiket') or k.get('text') or f"{k['q_num']}."
        CozumPenceresi(self.root, self.pdf_doc, self.pdf_kilit, self.aktif_sayfa, k['bbox_pdf'], f"{etiket} - Sayfa {self.aktif_sayfa + 1}")

    def kutulari_kaydet(self):
        if not any(self.tum_kutular.values()): return
        dosya = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("JSON", "*.json")], initialfile="soru_kutulari.json")
        if not dosya: return
        kayit = {
            'pdf_yolu': self.pdf_yolu, 'pdf_sayfa_sayisi': self.toplam_sayfa,
            'marj_varsayilan_tek': dict(self.marj_varsayilan_tek), 'marj_varsayilan_cift': dict(self.marj_varsayilan_cift),
            'marj_sayfa': {str(p): dict(v) for p, v in self.marj_sayfa.items()},
            'baslik_marj_varsayilan_tek': dict(self.baslik_marj_varsayilan_tek), 'baslik_marj_varsayilan_cift': dict(self.baslik_marj_varsayilan_cift),
            'baslik_marj_sayfa': {str(p): dict(v) for p, v in self.baslik_marj_sayfa.items()},
            'sayfa_bilgi': {str(p): b for p, b in self.sayfa_bilgi.items()}, 'test_zorla': {str(p): v for p, v in self.test_zorla.items()},
            'sayfalar': []
        }
        for sayfa_no in sorted(self.tum_kutular):
            kutular = self.tum_kutular[sayfa_no]
            if not kutular: continue
            kayit['sayfalar'].append({'sayfa': sayfa_no + 1, 'kutular': [
                {'q_num': k['q_num'], 'bbox_pdf': [round(v, 2) for v in k['bbox_pdf']], 'text': k.get('etiket') or k.get('text', ''), 'cevap': k.get('cevap', ''), 'ortak_rect': [round(v, 2) for v in k['ortak_rect']] if k.get('ortak_rect') else None} for k in kutular]})
        with open(dosya, 'w', encoding='utf-8') as f: json.dump(kayit, f, ensure_ascii=False, indent=2)
        messagebox.showinfo("Basarili", f"Kaydedildi:\n{dosya}")

    def kutulari_yukle(self):
        dosya = filedialog.askopenfilename(filetypes=[("JSON", "*.json")])
        if not dosya: return
        self.kutulari_yukle_json(dosya)

    def kutulari_yukle_json(self, dosya):
        try:
            with open(dosya, 'r', encoding='utf-8') as f: kayit = json.load(f)
        except Exception as e: messagebox.showerror("Hata", str(e)); return
        pdf_yolu = kayit.get('pdf_yolu')
        if not pdf_yolu or not os.path.isfile(pdf_yolu):
            pdf_yolu = filedialog.askopenfilename(filetypes=[("PDF Dosyalari", "*.pdf")])
            if not pdf_yolu: return
        with self.pdf_kilit:
            self.pdf_yolu = pdf_yolu
            if self.pdf_doc is not None:
                try: self.pdf_doc.close()
                except: pass
            self.pdf_doc = fitz.open(pdf_yolu); self.toplam_sayfa = len(self.pdf_doc)

        def _g_marj(m):
            try: return {k: float(m[k]) for k in ('ust', 'alt', 'sol', 'sag')}
            except: return None

        self.marj_varsayilan_tek = _g_marj(kayit.get('marj_varsayilan_tek')) or dict(MARJ_VARSAYILAN_TEK)
        self.marj_varsayilan_cift = _g_marj(kayit.get('marj_varsayilan_cift')) or dict(MARJ_VARSAYILAN_CIFT)
        self.marj_sayfa = {int(k): _g_marj(v) for k, v in (kayit.get('marj_sayfa') or {}).items() if _g_marj(v)}
        self.baslik_marj_varsayilan_tek = _g_marj(kayit.get('baslik_marj_varsayilan_tek')) or dict(MARJ_BASLIK_VARSAYILAN_TEK)
        self.baslik_marj_varsayilan_cift = _g_marj(kayit.get('baslik_marj_varsayilan_cift')) or dict(MARJ_BASLIK_VARSAYILAN_CIFT)
        self.baslik_marj_sayfa = {int(k): _g_marj(v) for k, v in (kayit.get('baslik_marj_sayfa') or {}).items() if _g_marj(v)}

        self.tum_kutular = {}
        for sayfa_kayit in kayit.get('sayfalar', []):
            pno = int(sayfa_kayit.get('sayfa', 0)) - 1
            if pno < 0 or pno >= self.toplam_sayfa: continue
            kutular = []
            for k in sayfa_kayit.get('kutular', []):
                kk = {'q_num': int(k['q_num']), 'bbox_pdf': [float(v) for v in k['bbox_pdf']], 'text': k.get('text', '')}
                if k.get('cevap'): kk['cevap'] = k['cevap']
                if k.get('ortak_rect'): kk['ortak_rect'] = [float(v) for v in k['ortak_rect']]
                kutular.append(kk)
            if kutular: self.tum_kutular[pno] = kutular

        self.sayfa_bilgi = {int(k): v for k, v in (kayit.get('sayfa_bilgi') or {}).items()}
        self.test_zorla = {int(k): (None if v is None else bool(v)) for k, v in (kayit.get('test_zorla') or {}).items()}
        self.aktif_sayfa = 0; self.secili = None; self.secili_ortak = False; self.ortak_aktif = None
        self.listeyi_guncelle(); self.sigdir(); self._marj_ciz()


if __name__ == "__main__":
    import traceback
    import tkinter.messagebox as mb
    try:
        root = tk.Tk()
        app = PDFTopluSoruIsaretleyici(root)
        root.mainloop()
    except Exception as e:
        hata_detayi = traceback.format_exc()
        mb.showerror("Uygulama Başlatma Hatası", f"Uygulama başlatılırken bir hata oluştu:\n\n{e}\n\nDetaylar:\n{hata_detayi}")
