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
EXTRACTION_PROMPT = """Tu reçois un document fiscal français (avis d'imposition, déclaration 2042, ou photo d'un tel document).

Extrais les données fiscales suivantes et retourne-les dans un JSON strict.
Si une valeur est absente ou illisible, mets null.
Ne retourne RIEN d'autre que le JSON — pas de texte, pas d'explication.

{
  "situation": "celibataire|marie|pacse|divorce|veuf",
  "parts": <nombre de parts fiscales, ex: 2.5>,
  "rfr": <revenu fiscal de référence, entier en €>,
  "revenu_imposable": <revenu net imposable, entier en €>,
  "salaire_1": <salaires déclarant 1 / case 1AJ, entier en €>,
  "salaire_2": <salaires déclarant 2 / case 1BJ conjoint, entier en €>,
  "ir_net": <impôt sur le revenu net, entier en €>,
  "revenus_tns": <bénéfices BIC/BNC/BA pour TNS/indépendant, entier en €>,
  "plafond_annuel": <plafond PER disponible année en cours, entier en €>,
  "report_n1": <plafond PER non utilisé N-1, entier en €>,
  "report_n2": <plafond PER non utilisé N-2, entier en €>,
  "report_n3": <plafond PER non utilisé N-3, entier en €>
}

Indices pour localiser les données :
- "Situation de famille" ou "Marié/Pacsé/Célibataire" → situation
- "Nombre de parts" ou "Quotient familial" → parts
- "Revenu fiscal de référence" ou "RFR" → rfr
- "Revenu net imposable" → revenu_imposable
- Case 1AJ (ou "Traitements, salaires") → salaire_1
- Case 1BJ → salaire_2
- "Impôt net" ou "Montant de l'impôt" → ir_net
- "BIC", "BNC", "BA", "bénéfices" → revenus_tns
- Section "Plafonds de déductibilité épargne retraite" ou "Épargne retraite" → plafond_annuel, report_n1, report_n2, report_n3
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

    # Situation familiale
    m = find_first([r'(mari[eé]e?|pacs[eé]e?|c[eé]libataire|divorc[eé]e?|veuf|veuve)'], text)
    if m:
        s = m.group(1).lower()
        if 'mari' in s or 'pacs' in s:
            data['situation'] = 'marie'
        elif 'divorc' in s:
            data['situation'] = 'divorce'
        elif 'veuf' in s or 'veuve' in s:
            data['situation'] = 'veuf'
        else:
            data['situation'] = 'celibataire'
        fields_found.append('Situation familiale')

    # Parts
    m = find_first([
        r'nombre\s+de\s+parts?\s*(?:du\s+foyer)?\s*[:\s=]+\s*(\d+[,\.]\d+|\d+)',
        r'quotient\s+familial[^\d]*(\d+[,\.]\d+)',
        r'(\d+[,\.]\d+)\s+parts?',
    ], text)
    if m:
        data['parts'] = float(m.group(1).replace(',', '.'))
        fields_found.append('Nombre de parts')

    # RFR
    m = find_first([
        r'revenu\s+fiscal\s+de\s+r[eé]f[eé]rence\s*[:\s]*(\d[\d\s ]{2,10})',
        r'r\.f\.r\.?\s*[:\s]*(\d[\d\s ]{2,10})',
    ], text)
    if m:
        n = clean_number(m.group(1)[:12])
        if n:
            data['rfr'] = n
            fields_found.append('Revenu fiscal de référence')

    # Revenu imposable
    m = find_first([
        r'revenu\s+net\s+imposable\s*[:\s]*(\d[\d\s ]{2,10})',
        r'revenu\s+imposable\s*[:\s]*(\d[\d\s ]{2,10})',
    ], text)
    if m:
        n = clean_number(m.group(1)[:12])
        if n:
            data['revenu_imposable'] = n
            fields_found.append('Revenu net imposable')

    # Salaires 1AJ / 1BJ
    m = find_first([
        r'1\s*AJ\s*[:\s]*(\d[\d\s ]{2,10})',
        r'traitements?\s*[,;]\s*salaires?[^\d]{0,40}(\d[\d\s ]{3,10})',
    ], text)
    if m:
        n = clean_number(m.group(1)[:12])
        if n and n > 1000:
            data['salaire_1'] = n
            fields_found.append('Salaires déclarant')
    m = find_first([r'1\s*BJ\s*[:\s]*(\d[\d\s ]{2,10})'], text)
    if m:
        n = clean_number(m.group(1)[:12])
        if n and n > 1000:
            data['salaire_2'] = n
            fields_found.append('Salaires conjoint')

    # IR net
    m = find_first([
        r'imp[oô]t\s+(?:sur\s+le\s+revenu\s+)?net\s*[:\s]*(\d[\d\s ]{0,10})',
        r'montant\s+(?:de\s+)?l\'imp[oô]t\s*[:\s]*(\d[\d\s ]{0,10})',
    ], text)
    if m:
        n = clean_number(m.group(1)[:10])
        if n is not None:
            data['ir_net'] = n
            fields_found.append('Impôt net')

    # BIC/BNC/BA
    m = find_first([
        r'b[eé]n[eé]fices?\s+industriels?[^\d]{0,30}(\d[\d\s ]{2,10})',
        r'\bBIC\b\s*[:\s]*(\d[\d\s ]{2,10})',
        r'b[eé]n[eé]fices?\s+non\s+commerciaux[^\d]{0,30}(\d[\d\s ]{2,10})',
        r'\bBNC\b\s*[:\s]*(\d[\d\s ]{2,10})',
        r'b[eé]n[eé]fices?\s+agricoles?[^\d]{0,30}(\d[\d\s ]{2,10})',
    ], text)
    if m:
        n = clean_number(m.group(1)[:12])
        if n and n > 1000:
            data['revenus_tns'] = n
            fields_found.append('Bénéfices TNS')

    # Plafonds PER
    section_m = re.search(
        r'(?:plafonds?\s+(?:de\s+)?d[eé]ductibilit[eé][^\n]*[eé]pargne'
        r'|[eé]pargne.{0,5}retraite[^\n]*plafond'
        r'|plafonds?\s+[eé]pargne\s+retraite)(.{20,1200})',
        text, re.IGNORECASE | re.DOTALL
    )
    if not section_m:
        section_m = re.search(r'[eé]pargne\s+retraite(.{20,600})', text, re.IGNORECASE | re.DOTALL)

    if section_m:
        section = section_m.group(1)
        year_amounts = re.findall(r'(?:20(\d{2}))[^\d]{0,30}(\d[\d\s ]{1,8})\s*€?', section)
        amounts_by_year = {}
        for yr_s, amt_s in year_amounts:
            yr = int('20' + yr_s)
            n = clean_number(amt_s)
            if n and 100 < n < 200000:
                amounts_by_year[yr] = n
        if amounts_by_year:
            cur = max(amounts_by_year.keys())
            for key, delta in [('plafond_annuel', 0), ('report_n1', 1), ('report_n2', 2), ('report_n3', 3)]:
                if (cur - delta) in amounts_by_year:
                    data[key] = amounts_by_year[cur - delta]
            fields_found.append('Plafonds épargne retraite')

    data['_fields_found'] = fields_found
    return data


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

        return jsonify({
            'success': True,
            'data': data,
            'fields_found': fields_found,
            'fields_count': len(fields_found),
            'method': method,  # 'text' ou 'vision'
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
