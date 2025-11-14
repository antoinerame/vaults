# Morpho vault explorer

Petit outil Flask qui s'appuie sur `vaults.py` pour :

- recuperer l'historique `sharePriceUsd` d'un vault Morpho entre deux dates ou sur tout l'historique via le bouton `TOUT`,
- afficher un graphique interactif (Chart.js) avec raccourcis (7j / 30j / ...),
- lister les vaults lies a un curator (via son slug ou une adresse 0x) et charger l'un d'eux en un clic, avec badge whitelist et TVL mise en forme,
- afficher une fiche detaillee pour le vault courant (TVL, APY, ratios de liquidite, composition par marche + alertes risque),
- tracer l'evolution du share price et resumer la TVL sur 30 jours (PnL, annualise, drawdown, decomposition flows/PnL),
- fournir un lien direct vers la page Morpho officielle pour ouvrir rapidement `https://app.morpho.org/{network}/vault/{address}`.

## Installation rapide

```bash
python -m venv .venv
.venv\Scripts\activate  # sous PowerShell
pip install -r requirements.txt
```

## Demarrer l'interface web

```bash
flask --app app run --debug
# ou : python app.py
```

Ensuite ouvrez <http://127.0.0.1:5000>. Choisissez le reseau, saisissez l'adresse du vault et les dates (ou cliquez sur les boutons 7 j / 30 j / ...) ou utilisez `TOUT` pour l'historique complet. La section "Curator" liste les vaults d'un curator (slug ou adresse) et permet de charger l'un d'eux. La fiche "Vault actuel" recapitule TVL/APY/liquidite/composition avec alertes, "Performance & flows" affiche les derivees (PnL, drawdown, flows) et un resume TVL sur 30 j, tandis qu'un bouton ouvre `https://app.morpho.org/{network}/vault/{address}` dans un nouvel onglet.


