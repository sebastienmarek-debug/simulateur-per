# Simulateur PER — Optimisation Fiscale

Outil de conseil patrimonial pour l'optimisation fiscale du Plan d'Épargne Retraite (PER).

## Fonctionnalités

- Calcul du plafond de déduction (salarié, TNS, agriculteur)
- Prise en compte des reports sur 3 ans et mutualisation conjoint
- Comparaison déduction vs. non-déduction
- Projection du capital à la retraite
- Recommandation personnalisée selon la TMI actuelle et à la retraite
- Rapport imprimable client

## Stack

- **Backend** : Python / Flask
- **Frontend** : HTML / CSS / JavaScript (Chart.js)
- **Déploiement** : Railway

## Lancer en local

```bash
pip install -r requirements.txt
python app.py
```

Accéder à `http://localhost:5000`

## Déploiement Railway

Ce projet est configuré pour un déploiement automatique via Railway à chaque push sur `main`.

---

*Données fiscales 2024 — PASS 2024 : 46 368 € — Barème IR 2025*
