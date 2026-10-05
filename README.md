# imhd.sk odchody – Home Assistant

Skutočné odchody spojov MHD zo stránky imhd.sk (Bratislava). Môžete si vybrať, ako ich chcete používať, prípadne všetky spôsoby naraz:

| Spôsob | Spojenie | Kedy sa hodí |
|---|---|---|
| **Akcia** `imhd_sk.get_departures` | len pri volaní (~2–3 s) | skripty a automatizácie, „ako veľmi sa musím ponáhľať“ |
| **Senzory – priebežne** (push) | trvalé, server tlačí zmeny | dashboard s okamžitými zmenami |
| **Senzory – pravidelne** (polling) | raz za N minút, inak nič | dashboard bez trvalého spojenia |
| **Senzory – na požiadanie** | len na požiadanie, nikdy samostatne | keď chcete úplnú kontrolu nad tým, kedy sa údaje sťahujú |

Pri jednorazovom načítaní sa integrácia pripojí na imhd.sk a prihlási sa k odberu zastávky (`tabStart`). Potom zbiera správy `tabs`, ktoré chodia postupne po nástupištiach. Keď počas `settle` sekúnd (predvolene 2) nepríde žiadna ďalšia, odpojí sa. Odpojenie je zároveň odhlásenie.

> Neoficiálne. Používa rovnaké rozhranie ako web imhd.sk (socket.io na `/rt/sio2`). Môže to porušovať podmienky imhd.sk a môže sa to kedykoľvek rozbiť.

## Inštalácia

1. HACS → Custom repositories → URL adresa repozitára, kategória *Integration*. Potom v HACS vyhľadajte integráciu *imhd.sk odchody* a stiahnite ju (*Download*). Alebo skopírujte `custom_components/imhd_sk` do `config/custom_components/`.
2. Reštartujte Home Assistant.
3. Nastavenia → Zariadenia a služby → Pridať integráciu → *imhd.sk odchody* a vyberte:
   - **Len akcia get_departures**: bez polí, iba sprístupní akciu.
   - **Zastávka so senzormi**: zastávku vyberiete zo zoznamu (najbližšie k domovu sú hore, dá sa písať a hľadať), prípadne vložíte odkaz na zastávku z imhd.sk alebo jej číslo. Ďalej názov (prázdne = názov zastávky), spôsob aktualizácie (priebežne/pravidelne/na požiadanie), filter liniek a počet odchodov. Interval pollingu (predvolene 2 min) zmeníte v *Konfigurovať*.

Zoznam zastávok a písmená nástupíšť (A, B, …) pochádzajú z backendu open-source appky [Transi](https://github.com/magicsk/Transi) (`api.magicsk.eu/stops`). Keď nie je dostupný, zobrazí sa obyčajné textové pole, kam vložíte odkaz z imhd.sk, napr. `https://imhd.sk/ba/zastavka/Na-kri%C5%BEovatk%C3%A1ch/ca71b671897182838071cc`. Integrácia si z neho číslo zastávky (`st=…`) zistí sama.

Akcia je dostupná vždy, keď je pridaná aspoň jedna položka, teda aj keď máte len zastávky.

## Senzory

Pre každú zastávku vznikne zariadenie s nasledovnými senzormi:

- `sensor.<zastavka>_najblizsi_odchod`: timestamp najbližšieho odchodu zo všetkých nástupíšť (po filtri liniek).
- `sensor.<zastavka>_nastupiste_<X>`: najbližší odchod z nástupišťa, teda z jedného smeru. Pomenované písmenom ako na zastávke (Nástupište A, B…), ak ho poznáme, inak interným číslom. Vytvárajú sa automaticky, keď nástupište prvýkrát príde v dátach. Atribút `destinations` ukazuje, kam z neho spoje idú, aby sa dalo rozoznať, ktorý smer to je. Entitu si potom môžete v HA premenovať, napríklad na „Do mesta“. Vypnúť sa dajú v *Konfigurovať*.
- `sensor.<zastavka>_linka_<L>_<X>`: najbližší odchod linky L z nástupišťa X, teda linka v jednom smere (napr. „Linka 96 · A“ → Prokofievova). Vytvárajú sa automaticky pre každú kombináciu linky a nástupišťa, ktorá sa objaví v dátach. **Filter liniek** v nastaveniach obmedzí, pre ktoré linky vzniknú, aby pri veľkých zastávkach nevznikli desiatky entít. Vypnúť sa dajú v *Konfigurovať*.

Stav senzora je čas odchodu, HA ho zobrazí ako „za X minút“. Podrobnosti sú v atribútoch: `line`, `destination`, `minutes`, `delay`, `realtime`, `departures` (zoznam ďalších odchodov), `connected`, `last_update`, `info`. Senzory nástupíšť a liniek majú navyše `platform`, `platform_label` a `destinations` (kam spoje z nástupišťa idú).

Senzory liniek sa hodia aj do bežných kariet bez šablón, napríklad:

```yaml
type: entities
title: Na križovatkách
entities:
  - sensor.na_krizovatkach_linka_96_a
  - sensor.na_krizovatkach_linka_61_a
  - type: attribute
    entity: sensor.na_krizovatkach_linka_96_a
    attribute: delay
    name: Meškanie 96
    suffix: min
```

Diagnostický `binary_sensor.<zastavka>_pripojenie` (Pripojenie) je zapnutý, keď je zastávka pripojená a chodia čerstvé dáta. Atribúty: `mode`, `connected`, `last_update`, `reconnects` (koľkokrát sa spojenie obnovilo), `last_error`. Hodí sa na upozornenie, keď dáta dlhšie nechodia.

### Režim na požiadanie

Integrácia nesťahuje nič sama: ani pri štarte HA, ani periodicky. Odchody sa stiahnu len pri:

- stlačení tlačidla `button.<zastavka>_obnovit` (Obnoviť), ktoré vznikne spolu so zastávkou,
- spustení akcie `homeassistant.update_entity` na ktorýkoľvek `sensor.*` zastávky (binárny senzor pripojenia nestačí), napr. v automatizácii pri otvorení dverí.

Do prvého obnovenia (aj po reštarte HA) majú senzory stav „neznámy“. Senzor pripojenia ukazuje, či posledné obnovenie uspelo. Spoje, ktoré už odišli, z naposledy stiahnutých dát postupne miznú, ale nové sa bez obnovenia neobjavia.

Tlačidlo Obnoviť majú aj zastávky v polling režime. V push režime nie je, tam sú dáta vždy aktuálne.

Režim zastávky sa nedá zmeniť v *Konfigurovať*. Na zmenu zastávku odstráňte a pridajte znova.

### Odolnosť priebežného režimu (push)

- Po **akomkoľvek** odpojení (výpadok siete, ping timeout aj odpojenie zo strany imhd.sk) sa integrácia znova pripojí s narastajúcim odstupom 2 s → max. 60 s, bez limitu pokusov. Samotná knižnica python-socketio sa po odpojení serverom znova nepripája, preto to riadi integrácia.
- **Watchdog:** ak počas 5 minút neprídu žiadne dáta (bežne prichádzajú každú minútu), spojenie sa zruší a nadviaže nanovo.
- Pripojenie sa počíta za úspešné až po prvej tabuli, nie po samotnom spojení.
- Údaje sa medzi spojeniami nestrácajú, takže senzory pri krátkom výpadku neukážu prázdne dáta.

V polling režime sa senzor pripojenia vypne hneď po prvom neúspešnom stiahnutí, alebo keď nové dáta neprišli dlhšie ako dva intervaly.

Atribúty, ktoré sa menia každú minútu (`departures`, `minutes`, `last_update`, …), sa neukladajú do histórie HA, aby nenafukovali databázu.

V polling režime `homeassistant.update_entity` na senzor stiahne dáta hneď, mimo intervalu. Polling však beží celý deň. Ak chcete dáta len v určitom čase, napríklad ráno, použite radšej režim na požiadanie a obnovujte cez automatizáciu.

Markdown karta:

```yaml
type: markdown
title: Odchody
content: >
  {% for d in state_attr('sensor.domov_najblizsi_odchod', 'departures')[:6] %}
  **{{ d.line }}** → {{ d.destination }} · {{ d.minutes }} min
  {%- if d.delay > 0 %} (+{{ d.delay }}){% endif %}
  {%- if not d.realtime %} *(CP)*{% endif %}
  {% endfor %}
```

## Akcia `imhd_sk.get_departures`

| Pole | Povinné | Význam |
|---|---|---|
| `stop_id` | áno | Číslo zastávky (napr. `341`) alebo odkaz na zastávku z imhd.sk |
| `lines` | nie | `"9, 39"` alebo zoznam. Prázdne znamená všetky. |
| `platforms` | nie | Filter nástupíšť podľa interného čísla, napr. `"837"` (nie podľa písmena A, B). Odpoveď vždy obsahuje `platforms`: nástupište → ciele. Ak je zastávka v zozname Transi, obsahuje aj `platform_labels`: číslo → písmeno. |
| `limit` | nie | Max. počet odchodov (predvolené 10) |
| `timeout` | nie | Max. celkový čas v sekundách (predvolené 10) |
| `settle` | nie | Zber dát je hotový, ak počas toľkých sekúnd neprídu údaje o žiadnom ďalšom nástupišti (predvolené 2). Ak vám chýbajú spoje, zvýšte. |

Odpoveď:

```yaml
stop_id: 93
fetched_at: "2026-10-05T06:23:00+00:00"
departures:
  - line: "9"
    destination: Karlova Ves
    time: "2026-10-05T06:29:00+00:00"
    minutes: 6
    delay: 2          # min, záporné = predstih
    realtime: true    # false = len podľa cestovného poriadku
    platform: "1"
    vehicle: "7416"
    last_stop: Kollárovo nám.
    stuck: false
platforms:            # nástupište → ciele (z celej tabule, bez filtrov)
  "1": [Karlova Ves]
info: []              # oznamy imhd.sk, ak prišli
stop_name: "…"        # len ak zastávku pozná zoznam Transi
platform_labels: { "1": "A" }
```

Vyskúšať sa dá v časti Nástroje → Akcie.

## Príklad: notifikácia pri odchode z domu

```yaml
triggers:
  - trigger: state
    entity_id: binary_sensor.vchodove_dvere
    to: "on"
conditions:
  - condition: time
    after: "06:30:00"
    before: "09:00:00"
    weekday: [mon, tue, wed, thu, fri]
actions:
  - action: imhd_sk.get_departures
    data:
      stop_id: 93
      lines: "9, 39"
      limit: 3
    response_variable: board
  - action: notify.mobile_app_telefon
    data:
      message: >
        {% for d in board.departures %}
        {{ d.line }} → {{ d.destination }} o {{ d.minutes }} min{% if d.delay > 0 %} (+{{ d.delay }}){% endif %}
        {% else %}Žiadne odchody.{% endfor %}
```

## Príklad: template senzor nad akciou

Ďalšia alternatíva k senzorom integrácie, ak chcete plnú kontrolu nad tým, kedy sa sťahuje:

```yaml
template:
  - triggers:
      - trigger: time_pattern
        minutes: "/2"
    conditions:
      - condition: time
        after: "06:30:00"
        before: "09:00:00"
    actions:
      - action: imhd_sk.get_departures
        data: { stop_id: 93, limit: 5 }
        response_variable: board
    sensor:
      - name: Zastávka domov
        device_class: timestamp
        state: "{{ board.departures[0].time if board.departures else none }}"
        attributes:
          departures: "{{ board.departures }}"
```

Atribút `departures` sa pri template senzore na rozdiel od senzorov integrácie ukladá do histórie HA a pri každom obnovení nafukuje databázu. Ak vám to prekáža, vylúčte senzor z recorderu (`recorder:` → `exclude:` → `entities:`).

## Testy

```
pip install pytest-homeassistant-custom-component "python-socketio[asyncio_client]"
pytest tests
```
