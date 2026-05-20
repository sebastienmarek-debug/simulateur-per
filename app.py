from flask import Flask, render_template, request, jsonify
import os
import io
import re

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 15 * 1024 * 1024  # 15 MB max

try:
    import pdfplumber
    PDF_SUPPORT = True
except ImportError:
    PDF_SUPPORT = False


# ─────────────────────────────────────────────
# Extraction de texte
# ─────────────────────────────────────────────

def extract_text_from_pdf(pdf_bytes):
    """Extrait le texte brut de toutes les pages du PDF."""
    pages_text = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            text = page.extract_text(x_tolerance=2, y_tolerance=2) or ''
            pages_text.append(text)
    return '\n'.join(pages_text)


def clean_number(raw):
    """Nettoie une chaîne et retourne un entier, ou None."""
    cleaned = re.sub(r'[\s ]', '', str(raw))  # espaces + espace insécable
    cleaned = re.sub(r'[^\d]', '', cleaned)
    return int(cleaned) if cleaned else None


def find_first(patterns, text, flags=re.IGNORECASE):
    """Essaie plusieurs patterns, retourne le premier match ou None."""
    for pattern in patterns:
        m = re.search(pattern, text, flags)
        if m:
            return m
    return None


# ─────────────────────────────────────────────
# Parseurs spécialisés
# ─────────────────────────────────────────────

def parse_situation(text):
    """Détecte la situation familiale."""
    m = find_first([
        r'(mari[eé]e?|pacs[eé]e?|c[eé]libataire|divorc[eé]e?|veuf|veuve)',
    ], text)
    if not m:
        return None
    s = m.group(1).lower()
    if 'mari' in s or 'pacs' in s:
        return 'marie'
    if 'divorc' in s:
        return 'divorce'
    if 'veuf' in s or 'veuve' in s:
        return 'veuf'
    return 'celibataire'


def parse_parts(text):
    """Extrait le nombre de parts fiscales."""
    m = find_first([
        r'nombre\s+de\s+parts?\s*(?:du\s+foyer)?\s*[:\s=]+\s*(\d+[,\.]\d+|\d+)',
        r'quotient\s+familial[^\d]*(\d+[,\.]\d+)',
        r'(\d+[,\.]\d+)\s+parts?',
    ], text)
    if m:
        return float(m.group(1).replace(',', '.'))
    return None


def parse_rfr(text):
    """Extrait le revenu fiscal de référence."""
    m = find_first([
        r'revenu\s+fiscal\s+de\s+r[eé]f[eé]rence\s*[:\s]*(\d[\d\s ]{2,10})',
        r'r\.f\.r\.?\s*[:\s]*(\d[\d\s ]{2,10})',
        r'rfr\s*[:\s]*(\d[\d\s ]{2,10})',
    ], text)
    if m:
        return clean_number(m.group(1)[:12])
    return None


def parse_revenu_imposable(text):
    """Extrait le revenu net imposable."""
    m = find_first([
        r'revenu\s+net\s+imposable\s*[:\s]*(\d[\d\s ]{2,10})',
        r'revenu\s+imposable\s*[:\s]*(\d[\d\s ]{2,10})',
        r'net\s+imposable\s*[:\s]*(\d[\d\s ]{2,10})',
    ], text)
    if m:
        return clean_number(m.group(1)[:12])
    return None


def parse_salaires(text):
    """Extrait les salaires déclarés (déclarant 1 et 2)."""
    result = {}

    # Déclarant 1 — case 1AJ
    m = find_first([
        r'1\s*AJ\s*[:\s]*(\d[\d\s ]{2,10})',
        r'traitements?\s*[,;]\s*salaires?[^\d]{0,40}(\d[\d\s ]{3,10})',
        r'salaires?\s+d[eé]clar[eé]s?[^\d]{0,20}(\d[\d\s ]{3,10})',
    ], text)
    if m:
        n = clean_number(m.group(1)[:12])
        if n and n > 1000:
            result['salaire_1'] = n

    # Déclarant 2 — case 1BJ
    m = find_first([
        r'1\s*BJ\s*[:\s]*(\d[\d\s ]{2,10})',
    ], text)
    if m:
        n = clean_number(m.group(1)[:12])
        if n and n > 1000:
            result['salaire_2'] = n

    return result


def parse_ir_net(text):
    """Extrait l'impôt sur le revenu net."""
    m = find_first([
        r'imp[oô]t\s+(?:sur\s+le\s+revenu\s+)?net\s*[:\s]*(\d[\d\s ]{0,10})',
        r'imp[oô]t\s+net\s+[àa]\s+payer\s*[:\s]*(\d[\d\s ]{0,10})',
        r'total\s+(?:de\s+)?l\'imp[oô]t\s*[:\s]*(\d[\d\s ]{0,10})',
        r'imp[oô]t\s+2\d{3}\s*[:\s]*(\d[\d\s ]{0,10})',
    ], text)
    if m:
        return clean_number(m.group(1)[:10])
    return None


def parse_bic_bnc(text):
    """Extrait les bénéfices TNS (BIC/BNC/BA)."""
    result = {}
    m = find_first([
        r'b[eé]n[eé]fices?\s+industriels?\s+et\s+commerciaux[^\d]{0,30}(\d[\d\s ]{2,10})',
        r'BIC\s*[:\s]*(\d[\d\s ]{2,10})',
        r'b[eé]n[eé]fices?\s+non\s+commerciaux[^\d]{0,30}(\d[\d\s ]{2,10})',
        r'BNC\s*[:\s]*(\d[\d\s ]{2,10})',
        r'b[eé]n[eé]fices?\s+agricoles?[^\d]{0,30}(\d[\d\s ]{2,10})',
        r'BA\s*[:\s]*(\d[\d\s ]{2,10})',
    ], text)
    if m:
        n = clean_number(m.group(1)[:12])
        if n and n > 1000:
            result['revenus_tns'] = n
    return result


def parse_plafonds_per(text):
    """
    Extrait les plafonds épargne retraite et les reports des années N-1/N-2/N-3.
    Section généralement intitulée 'PLAFONDS DE DÉDUCTIBILITÉ ÉPARGNE RETRAITE'
    ou 'Vos plafonds épargne retraite'.
    """
    result = {}

    # Localiser la section PER
    section_match = re.search(
        r'(?:plafonds?\s+(?:de\s+)?d[eé]ductibilit[eé][^\n]*[eé]pargne[^\n]*retraite'
        r'|[eé]pargne.{0,5}retraite[^\n]*plafond'
        r'|plafonds?\s+[eé]pargne\s+retraite)'
        r'(.{20,1500})',
        text,
        re.IGNORECASE | re.DOTALL
    )

    if not section_match:
        # Fallback: chercher "épargne retraite" suivi de montants
        section_match = re.search(
            r'[eé]pargne\s+retraite(.{20,800})',
            text,
            re.IGNORECASE | re.DOTALL
        )

    if section_match:
        section = section_match.group(1)

        # Chercher les montants associés aux années
        year_amounts = re.findall(
            r'(?:20(\d{2}))[^\d]{0,30}(\d[\d\s ]{1,8})\s*€?',
            section
        )

        amounts_by_year = {}
        for yr_suffix, amount_str in year_amounts:
            yr = int('20' + yr_suffix)
            n = clean_number(amount_str)
            if n and 100 < n < 200000:
                amounts_by_year[yr] = n

        if amounts_by_year:
            current = max(amounts_by_year.keys())
            if current in amounts_by_year:
                result['plafond_annuel'] = amounts_by_year[current]
            if (current - 1) in amounts_by_year:
                result['report_n1'] = amounts_by_year[current - 1]
            if (current - 2) in amounts_by_year:
                result['report_n2'] = amounts_by_year[current - 2]
            if (current - 3) in amounts_by_year:
                result['report_n3'] = amounts_by_year[current - 3]

        # Fallback: extraire les montants en ordre d'apparition
        if not result:
            all_amounts = re.findall(r'(\d[\d\s ]{2,8})\s*€', section)
            valid = [clean_number(a) for a in all_amounts if clean_number(a) and 100 < clean_number(a) < 200000]
            if valid:
                result['plafond_annuel'] = valid[0]
            if len(valid) > 1:
                result['report_n1'] = valid[1]
            if len(valid) > 2:
                result['report_n2'] = valid[2]
            if len(valid) > 3:
                result['report_n3'] = valid[3]

    return result


# ─────────────────────────────────────────────
# Orchestrateur principal
# ─────────────────────────────────────────────

def parse_avis_imposition(pdf_bytes):
    """Parse un avis d'imposition ou déclaration 2042 et retourne les champs clés."""
    text = extract_text_from_pdf(pdf_bytes)

    if len(text.strip()) < 100:
        raise ValueError(
            "Le PDF semble être une image scannée (pas de texte extractible). "
            "Utilisez un PDF natif issu de impots.gouv.fr."
        )

    data = {}
    fields_found = []

    # Situation familiale
    situation = parse_situation(text)
    if situation:
        data['situation'] = situation
        fields_found.append('Situation familiale')

    # Nombre de parts
    parts = parse_parts(text)
    if parts:
        data['parts'] = parts
        fields_found.append('Nombre de parts')

    # RFR
    rfr = parse_rfr(text)
    if rfr:
        data['rfr'] = rfr
        fields_found.append('Revenu fiscal de référence')

    # Revenu imposable
    rev_imp = parse_revenu_imposable(text)
    if rev_imp:
        data['revenu_imposable'] = rev_imp
        fields_found.append('Revenu net imposable')

    # Salaires
    salaires = parse_salaires(text)
    if salaires:
        data.update(salaires)
        fields_found.append('Salaires déclarés')

    # IR net
    ir = parse_ir_net(text)
    if ir is not None:
        data['ir_net'] = ir
        fields_found.append('Impôt net')

    # BIC/BNC/BA
    tns = parse_bic_bnc(text)
    if tns:
        data.update(tns)
        fields_found.append('Bénéfices professionnels (TNS)')

    # Plafonds PER
    plafonds = parse_plafonds_per(text)
    if plafonds:
        data.update(plafonds)
        fields_found.append('Plafonds épargne retraite')

    data['_fields_found'] = fields_found
    data['_text_length'] = len(text)

    return data


# ─────────────────────────────────────────────
# Routes Flask
# ─────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/parse-pdf', methods=['POST'])
def parse_pdf():
    if not PDF_SUPPORT:
        return jsonify({'error': 'Module pdfplumber non installé sur le serveur.'}), 500

    if 'file' not in request.files:
        return jsonify({'error': 'Aucun fichier reçu.'}), 400

    f = request.files['file']
    if not f.filename.lower().endswith('.pdf'):
        return jsonify({'error': 'Seuls les fichiers PDF sont acceptés.'}), 400

    try:
        pdf_bytes = f.read()
        data = parse_avis_imposition(pdf_bytes)
        return jsonify({
            'success': True,
            'data': data,
            'fields_found': data.get('_fields_found', []),
            'fields_count': len(data.get('_fields_found', [])),
        })
    except ValueError as e:
        return jsonify({'error': str(e)}), 422
    except Exception as e:
        return jsonify({'error': f'Erreur d\'analyse : {str(e)}'}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
