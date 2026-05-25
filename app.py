import os
import io
import re
import json
import base64

from flask import Flask, render_template, request, jsonify

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 25 * 1024 * 1024  # 25 MB

# ─────────────────────────────────────────────
# Dépendances optionnelles
# ─────────────────────────────────────────────
try:
    import pdfplumber
    PDFPLUMBER_OK = True
except ImportError:
    PDFPLUMBER_OK = False

try:
    from pdf2image import convert_from_bytes
    from PIL import Image
    PDF2IMAGE_OK = True
except ImportError:
    PDF2IMAGE_OK = False

try:
    import anthropic
    ANTHROPIC_OK = True
except ImportError:
    ANTHROPIC_OK = False

# ─────────────────────────────────────────────
# Prompt Claude Vision
# ─────────────────────────────────────────────
EXTRACTION_PROMPT = """Tu reçois un avis d'imposition français ou une déclaration de revenus (2042).
Extrais EXACTEMENT les champs ci-dessous. Si une valeur est absente ou illisible, mets null.
Retourne UNIQUEMENT le JSON — aucun texte avant ou après.

{
  "nom_declarant1": <prénom + nom du déclarant 1, chaîne>,
  "nom_declarant2": <prénom + nom du déclarant 2 si présent, chaîne ou null>,
  "situation": <"celibataire" | "marie" | "pacse" | "divorce" | "veuf">,
  "parts": <nombre de parts fiscales du foyer, nombre décimal ex: 2.5>,
  "rfr": <Revenu fiscal de référence, entier €>,
  "revenu_imposable": <Revenu net imposable (ligne "Revenu imposable"), entier €>,
  "salaire_1": <Traitements/salaires bruts déclarant 1 AVANT abattement 10%, case 1AJ, entier €>,
  "salaire_2": <Traitements/salaires bruts déclarant 2 AVANT abattement 10%, case 1BJ, entier €>,
  "revenus_gerant": <Revenus des associés et gérants (art. 62 CGI), entier € ou null>,
  "revenus_bnc": <BNC professionnels déclarés ou imposables, entier € ou null>,
  "revenus_bic": <BIC professionnels, entier € ou null>,
  "revenus_ba":  <Bénéfices agricoles, entier € ou null>,
  "revenus_fonciers": <Revenus fonciers nets, entier €>,
  "ir_net": <Total impôt sur le revenu NET (après réductions et crédits), entier €>,
  "tmi_declare": <Taux marginal d'imposition indiqué sur l'avis, ex: 30 pour 30%>,
  "plafond_total_per": <Plafond TOTAL disponible tous déclarants "Plafond pour les cotisations versées en AAAA", entier €>,
  "plafond_d1": <Plafond PER déclarant 1 uniquement, entier €>,
  "plafond_d2": <Plafond PER déclarant 2 uniquement, entier €>,
  "report_n1_d1": <Plafond non utilisé N-1 déclarant 1, entier €>,
  "report_n1_d2": <Plafond non utilisé N-1 déclarant 2, entier €>,
  "report_n2_d1": <Plafond non utilisé N-2 déclarant 1, entier €>,
  "report_n2_d2": <Plafond non utilisé N-2 déclarant 2, entier €>
}

RÈGLES IMPORTANTES :
1. situation : si "Déclarant 2" est présent dans le document → "marie" (ou "pacse" si mentionné). Sinon "celibataire".
2. revenus_gerant : cherche "Revenus des associés et gérants", "gérants de SARL", "art. 62" → c'est un revenu TNS.
3. revenus_bnc : cherche "BNC professionnels déclarés", "BNC pro.", "bénéfices non commerciaux".
4. ir_net : cherche "Total de l'impôt sur le revenu net" ou "IMPOT NET". Ne pas confondre avec l'impôt avant réductions.
5. tmi_declare : cherche "Taux marginal d'imposition" suivi d'un pourcentage.
6. plafonds PER : section "PLAFOND EPARGNE RETRAITE". Additionner D1+D2 pour plafond_total_per. Ignorer la colonne Enfant.
7. salaire_1/salaire_2 : prendre le montant AVANT abattement (case 1AJ/1BJ), pas le net après déduction.
"""

# ─────────────────────────────────────────────
# Extraction texte (PDF natif — rapide)
# ─────────────────────────────────────────────
def extract_text_pdfplumber(pdf_bytes):
    if not PDFPLUMBER_OK:
        return ''
    pages = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            pages.append(page.extract_text(x_tolerance=2, y_tolerance=2) or '')
    return '\n'.join(pages)


def text_has_content(text, min_chars=200):
    return len(text.strip()) >= min_chars


# ─────────────────────────────────────────────
# Parseurs regex (pour PDF texte)
# ─────────────────────────────────────────────
def clean_number(raw):
    cleaned = re.sub(r'[\s ]', '', str(raw))
    cleaned = re.sub(r'[^\d]', '', cleaned)
    return int(cleaned) if cleaned else None


def find_first(patterns, text, flags=re.IGNORECASE):
    for pattern in patterns:
        m = re.search(pattern, text, flags)
        if m:
            return m
    return None


def parse_with_regex(text):
    data = {}
    fields_found = []

    # ── Noms des déclarants ──────────────────────────────────────────
    # [A-Z ]+ sans \n pour ne pas déborder sur la ligne suivante
    m = re.search(r'D[eé]clarant\s+1\s*-\s*Nom\s+de\s+naissance\s*[:\s]+([A-Z][A-Z ]+)', text)
    if m:
        data['nom_declarant1'] = m.group(1).strip()
        fields_found.append('Nom déclarant 1')
    m2 = re.search(r'D[eé]clarant\s+2\s*-\s*Nom\s+de\s+naissance\s*[:\s]+([A-Z][A-Z ]+)', text)
    if m2:
        data['nom_declarant2'] = m2.group(1).strip()
        fields_found.append('Nom déclarant 2')

    # ── Situation familiale ──────────────────────────────────────────
    m = find_first([r'(mari[eé]e?|pacs[eé]e?|c[eé]libataire|divorc[eé]e?|veuf|veuve)'], text)
    if m:
        s = m.group(1).lower()
        if 'mari' in s:      data['situation'] = 'marie'
        elif 'pacs' in s:    data['situation'] = 'pacse'
        elif 'divorc' in s:  data['situation'] = 'divorce'
        elif 'veuf' in s:    data['situation'] = 'veuf'
        else:                  data['situation'] = 'celibataire'
        fields_found.append('Situation familiale')
    elif re.search(r'D[eé]clarant\s+2', text):
        data['situation'] = 'marie'
        fields_found.append('Situation familiale')

    # ── Nombre de parts ─────────────────────────────────────────────
    m = find_first([
        r'nombre\s+de\s+parts?\s*(?:du\s+foyer)?\s*[:\s=]+\s*(\d+[,\.]\d+|\d+)',
        r'quotient\s+familial[^\d]*(\d+[,\.]\d+)',
        # Format avis d'imposition DGFiP : "M 1\n2,50" (M=marié, 1=page, 2,50=parts)
        r'\bM\s+\d+\s*\n\s*(\d+[,\.]\d+)',
        # Format alternatif : décimal autonome sur la ligne suivante un entier seul
        r'\n\d+\n(\d+[,\.]\d+)\n',
    ], text)
    if m:
        data['parts'] = float(m.group(1).replace(',', '.').replace(' ', ''))
        fields_found.append('Nombre de parts')

    # ── Revenu fiscal de référence ───────────────────────────────────
    m = find_first([
        r'revenu\s+fiscal\s+de\s+r[eé]f[eé]rence\s*\d*[.\s]*(\d[\d\s]{3,10})',
        r'\bRFR\b\s*[:\s]*(\d[\d\s]{3,10})',
    ], text)
    if m:
        n = clean_number(m.group(1)[:12])
        if n and n > 100:
            data['rfr'] = n
            fields_found.append('Revenu fiscal de référence')

    # ── Revenu imposable ────────────────────────────────────────────
    m = find_first([
        r'revenu\s+imposable[.\s]*(\d[\d\s]{3,10})',
        r'revenu\s+net\s+imposable[.\s]*(\d[\d\s]{3,10})',
    ], text)
    if m:
        n = clean_number(m.group(1)[:12])
        if n and n > 500:
            data['revenu_imposable'] = n
            fields_found.append('Revenu net imposable')

    # ── Salaires bruts (avant abattement) ───────────────────────────
    m = find_first([
        r'1\s*AJ\s*[:\s]*(\d[\d\s]{3,10})',
        r'traitements?[,;/]\s*salaires?[^\d]{0,50}(\d[\d\s]{3,10})',
    ], text)
    if m:
        n = clean_number(m.group(1)[:12])
        if n and n > 1000:
            data['salaire_1'] = n
            fields_found.append('Salaires déclarant')

    m = find_first([r'1\s*BJ\s*[:\s]*(\d[\d\s]{3,10})'], text)
    if m:
        n = clean_number(m.group(1)[:12])
        if n and n > 1000:
            data['salaire_2'] = n
            fields_found.append('Salaires conjoint')

    # ── Revenus des associés et gérants (art. 62) ────────────────────
    m = find_first([
        r'revenus?\s+des?\s+associ[eé]s?\s+et\s+g[eé]rants?[.\s]*(\d[\d\s]{3,10})',
        r'g[eé]rants?\s+(?:de\s+)?(?:sarl|sas|sasu)[.\s]*(\d[\d\s]{3,10})',
    ], text)
    if m:
        n = clean_number(m.group(1)[:12])
        if n and n > 1000:
            data['revenus_gerant'] = n
            data['statut_hint'] = 'tns'
            fields_found.append('Revenus gérant (TNS)')

    # ── BNC professionnels ──────────────────────────────────────────
    m = find_first([
        r'BNC\s+(?:pro(?:fessionnels?)?\s+)?(?:d[eé]clar[eé]s?|imposables?|hors\s+quotient)[.\s]*(\d[\d\s]{2,10})',
        r'b[eé]n[eé]fices?\s+non\s+commerciaux[^\d]{0,40}(\d[\d\s]{2,10})',
    ], text)
    if m:
        n = clean_number(m.group(1)[:12])
        if n and n > 100:
            data['revenus_bnc'] = n
            if 'statut_hint' not in data: data['statut_hint'] = 'tns'
            fields_found.append('BNC professionnels')

    # ── BIC professionnels ──────────────────────────────────────────
    m = find_first([
        r'BIC\s+(?:pro(?:fessionnels?)?\s+)?(?:d[eé]clar[eé]s?|imposables?)[.\s]*(\d[\d\s]{2,10})',
        r'b[eé]n[eé]fices?\s+industriels?\s+et\s+commerciaux[^\d]{0,40}(\d[\d\s]{2,10})',
    ], text)
    if m:
        n = clean_number(m.group(1)[:12])
        if n and n > 100:
            data['revenus_bic'] = n
            if 'statut_hint' not in data: data['statut_hint'] = 'tns'
            fields_found.append('BIC professionnels')

    # ── Bénéfices agricoles ─────────────────────────────────────────
    m = find_first([r'b[eé]n[eé]fices?\s+agricoles?[^\d]{0,30}(\d[\d\s]{2,10})'], text)
    if m:
        n = clean_number(m.group(1)[:12])
        if n and n > 100:
            data['revenus_ba'] = n
            if 'statut_hint' not in data: data['statut_hint'] = 'agriculteur'
            fields_found.append('Bénéfices agricoles')

    # ── Revenus fonciers nets ────────────────────────────────────────
    m = find_first([
        r'revenus?\s+fonciers?\s+nets?[.\s]*(\d[\d\s]{0,10})',
    ], text)
    if m:
        n = clean_number(m.group(1)[:10])
        if n is not None:
            data['revenus_fonciers'] = n
            fields_found.append('Revenus fonciers')

    # ── Impôt sur le revenu net (après réductions et crédits) ────────
    m = find_first([
        r'total\s+de\s+l[\'\u2019]imp[o\xf4]t\s+sur\s+le\s+revenu\s+net[.\s]*(\d[\d\s]{0,10})',
        r'IMPOT\s+NET[^0-9]{0,80}(\d[\d\s]{0,10})',
        r'imp[o\xf4]t\s+(?:sur\s+le\s+revenu\s+)?net[.\s]*(\d[\d\s]{0,10})',
    ], text)
    if m:
        n = clean_number(m.group(1)[:10])
        if n is not None:
            data['ir_net'] = n
            fields_found.append('Impôt net')

    # ── TMI déclaré sur l'avis ──────────────────────────────────────
    m = re.search(r'taux\s+marginal\s+d[\'\u2019]imposition[.\s%]*(\d+)[,.]?(\d*)\s*%?', text, re.IGNORECASE)
    if m:
        tmi_int = m.group(1)
        tmi_dec = m.group(2)
        tmi_val = float(tmi_int + ('.' + tmi_dec if tmi_dec else ''))
        data['tmi_declare'] = tmi_val
        fields_found.append(f'TMI déclaré ({tmi_val:.0f}%)')

    # ── Section Plafonds Épargne Retraite ────────────────────────────
    per_m = re.search(r'PLAFOND\s+EPARGNE\s+RETRAITE(.{50,2000}?)(?:\n[-\u2500]{10}|\Z)', text, re.IGNORECASE | re.DOTALL)
    if not per_m:
        per_m = re.search(r'plafond\s+[eé]pargne\s+retraite(.{50,1500})', text, re.IGNORECASE | re.DOTALL)

    if per_m:
        section = per_m.group(1)

        # Ligne finale "Plafond pour les cotisations versées en AAAA = D1 = D2 [= Enfant]"
        final_line = re.search(
            r'[Pp]lafond\s+pour\s+les\s+cotisations\s+vers[eé]es\s+en\s+\d{4}[^0-9]*([\d\s=+]{5,60})',
            section
        )
        if final_line:
            # Extraire TOUS les nombres y compris les zéros (D1 peut valoir 0)
            raw_nums = [clean_number(x) for x in re.findall(r'\d[\d\s]{0,8}', final_line.group(1))]
            raw_nums = [n for n in raw_nums if n is not None and n < 300000]
            # Limiter à 2 colonnes déclarants (ignorer colonne Enfant = 3e)
            d_nums = raw_nums[:2]
            if d_nums:
                total_per = sum(d_nums)
                data['plafond_total_per'] = total_per
                data['plafond_d1'] = d_nums[0] if len(d_nums) >= 1 else 0
                data['plafond_d2'] = d_nums[1] if len(d_nums) >= 2 else 0
                fields_found.append('Plafond PER total')

        # Reports par année
        report_years = [
            (r'non\s+utilis[eé]\s+pour\s+les\s+revenus\s+de\s+2024', 'report_n1_d1', 'report_n1_d2'),
            (r'non\s+utilis[eé]\s+pour\s+les\s+revenus\s+de\s+2023', 'report_n2_d1', 'report_n2_d2'),
            (r'non\s+utilis[eé]\s+pour\s+les\s+revenus\s+de\s+2022', 'report_n3_d1', 'report_n3_d2'),
        ]
        for label, k1, k2 in report_years:
            rm = re.search(label + r'[^0-9]*([\d\s+]{3,40})', section, re.IGNORECASE)
            if rm:
                rn = [clean_number(x) for x in re.findall(r'\d[\d\s]{0,8}', rm.group(1))]
                rn = [x for x in rn if x is not None and x < 200000]
                if len(rn) >= 1 and k1 not in data: data[k1] = rn[0]
                if len(rn) >= 2 and k2 not in data: data[k2] = rn[1]

    data['_fields_found'] = fields_found
    return data


def _consolidate_tns_income(data):
    total = sum((data.get(k) or 0) for k in ('revenus_gerant', 'revenus_bnc', 'revenus_bic', 'revenus_ba'))
    return total if total > 0 else None


def _consolidate_plafond(data):
    if data.get('plafond_total_per'):
        return data['plafond_total_per']
    return (data.get('plafond_d1') or 0) + (data.get('plafond_d2') or 0) or None



# ─────────────────────────────────────────────
# Extraction via Claude Vision (scan / image)
# ─────────────────────────────────────────────
def image_to_b64(img, max_dim=2000):
    """Redimensionne si nécessaire et encode en base64 JPEG."""
    w, h = img.size
    if max(w, h) > max_dim:
        ratio = max_dim / max(w, h)
        img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
    buf = io.BytesIO()
    img.convert('RGB').save(buf, format='JPEG', quality=88)
    return base64.standard_b64encode(buf.getvalue()).decode('utf-8')


def extract_with_claude_vision(images_b64):
    """Envoie les images de pages à Claude claude-sonnet-4-6 pour extraction."""
    client = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))

    content = []
    for i, b64 in enumerate(images_b64):
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": b64},
        })
        if len(images_b64) > 1:
            content.append({"type": "text", "text": f"— Page {i + 1} —"})

    content.append({"type": "text", "text": EXTRACTION_PROMPT})

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": content}],
    )

    raw = response.content[0].text.strip()

    # Extraire le JSON même si Claude ajoute du texte autour
    json_match = re.search(r'\{[\s\S]*\}', raw)
    if not json_match:
        raise ValueError(f"Réponse Claude non parseable : {raw[:200]}")

    extracted = json.loads(json_match.group())

    # Normaliser et construire _fields_found
    fields_found = []
    field_labels = {
        'situation': 'Situation familiale',
        'parts': 'Nombre de parts',
        'rfr': 'Revenu fiscal de référence',
        'revenu_imposable': 'Revenu net imposable',
        'salaire_1': 'Salaires déclarant',
        'salaire_2': 'Salaires conjoint',
        'ir_net': 'Impôt net',
        'revenus_tns': 'Bénéfices TNS',
        'plafond_annuel': 'Plafond PER annuel',
        'report_n1': 'Report N-1',
        'report_n2': 'Report N-2',
        'report_n3': 'Report N-3',
    }
    for k, label in field_labels.items():
        if extracted.get(k) is not None:
            fields_found.append(label)

    # Nettoyer les null
    data = {k: v for k, v in extracted.items() if v is not None}
    data['_fields_found'] = fields_found
    return data


# ─────────────────────────────────────────────
# Pipeline principal
# ─────────────────────────────────────────────
def pdf_to_images(pdf_bytes, dpi=180, max_pages=3):
    """Convertit les N premières pages d'un PDF en images PIL."""
    images = convert_from_bytes(pdf_bytes, dpi=dpi, first_page=1, last_page=max_pages)
    return images


def parse_document(file_bytes, filename):
    """
    Essaie dans l'ordre :
    1. pdfplumber sur PDF natif (texte sélectionnable)
    2. Claude Vision sur PDF converti en images (scan PDF)
    3. Claude Vision directement sur image brute (JPEG/PNG)
    Si pdfplumber extrait du texte lisible, on retourne toujours ce résultat
    (même partiel) plutôt que de planter sur poppler manquant.
    """
    ext = os.path.splitext(filename.lower())[1]
    is_pdf = ext == '.pdf'
    is_image = ext in ('.jpg', '.jpeg', '.png', '.webp', '.heic', '.bmp', '.tiff', '.tif')

    text_data = None  # résultat pdfplumber de secours

    # ── Étape 1 : PDF avec texte natif ──────────────────────────────
    if is_pdf and PDFPLUMBER_OK:
        text = extract_text_pdfplumber(file_bytes)
        if text_has_content(text, min_chars=200):
            data = parse_with_regex(text)
            if len(data.get('_fields_found', [])) >= 3:
                data['_method'] = 'text'
                return data
            # Texte trouvé mais peu de champs : garde en fallback, essaie Vision
            text_data = data

    # ── Étape 2 : Vision Claude ──────────────────────────────────────
    if not ANTHROPIC_OK:
        if text_data is not None:
            text_data['_method'] = 'text'
            return text_data
        raise ValueError("Module anthropic non disponible. Déposez une image JPEG/PNG.")

    try:
        if is_pdf:
            if not PDF2IMAGE_OK:
                raise RuntimeError("pdf2image non disponible")
            pil_images = pdf_to_images(file_bytes, dpi=180, max_pages=3)
            images_b64 = [image_to_b64(img) for img in pil_images]
        elif is_image:
            img = Image.open(io.BytesIO(file_bytes))
            images_b64 = [image_to_b64(img)]
        else:
            raise ValueError(f"Format non supporté : {ext}. Utilisez PDF, JPEG ou PNG.")

        data = extract_with_claude_vision(images_b64)
        data['_method'] = 'vision'
        return data

    except Exception as vision_err:
        # Si Vision échoue (poppler absent, API down…) mais qu'on a du texte, on retourne ça
        if text_data is not None:
            text_data['_method'] = 'text'
            return text_data
        if is_pdf:
            raise ValueError(
                f"Impossible d'analyser ce PDF (essayez une capture d'écran JPEG) : {vision_err}"
            )
        raise


# ─────────────────────────────────────────────
# Routes Flask
# ─────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html')


@app.route('/parse-pdf', methods=['POST'])
def parse_pdf():
    if 'file' not in request.files:
        return jsonify({'error': 'Aucun fichier reçu.'}), 400

    f = request.files['file']
    filename = f.filename or ''
    allowed = ('.pdf', '.jpg', '.jpeg', '.png', '.webp', '.bmp', '.tiff', '.tif')
    if not any(filename.lower().endswith(e) for e in allowed):
        return jsonify({'error': 'Format non supporté. Utilisez PDF, JPEG ou PNG.'}), 400

    try:
        file_bytes = f.read()
        data = parse_document(file_bytes, filename)
        method = data.pop('_method', 'text')
        fields_found = data.get('_fields_found', [])

        # Calcul des agrégats utiles pour le frontend
        tns_total = _consolidate_tns_income(data)
        if tns_total:
            data['revenus_tns'] = tns_total
        per_total = _consolidate_plafond(data)
        if per_total:
            data['plafond_annuel'] = per_total

        return jsonify({
            'success': True,
            'data': data,
            'fields_found': fields_found,
            'fields_count': len(fields_found),
            'method': method,
        })

    except json.JSONDecodeError as e:
        return jsonify({'error': f'Erreur de parsing JSON depuis Claude : {str(e)}'}), 500
    except ValueError as e:
        return jsonify({'error': str(e)}), 422
    except Exception as e:
        return jsonify({'error': f'Erreur inattendue : {str(e)}'}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
