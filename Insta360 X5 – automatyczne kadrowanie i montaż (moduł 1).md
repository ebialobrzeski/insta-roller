# Insta360 X5 – automatyczne kadrowanie i montaż (moduł 1)

Dokument projektu. Stan ustaleń z dyskusji z 23 listopada 2026.

## Cel i zakres

Aplikacja automatycznie przerabia nagrania 360° z Insta360 X5 na gotowe filmy.

- **Moduł 1 (ten dokument):** wybór fragmentów, kadrowanie i łączenie klipów.
- **Moduł 2 (później):** napisy, muzyka, ewentualne nakładki z danymi lotu (wariometr, wysokość).

Dwa główne typy materiału i dwa gotowe profile na jednym silniku, plus profil „inne” konfigurowany przy zadaniu (opisany niżej):

| Profil | Materiał | Format wyjścia |
| --- | --- | --- |
| Lot | Szybowiec lub motoszybowiec, kamera w kabinie | Pionowy 9:16 (rolka) |
| Urlop | Spacery z kijem do selfie | Poziomy 16:9 dopuszczalny |

## Sprzęt i architektura

> **Zmiana z 23 listopada 2026:** aplikacja jest samodzielnym programem na PC z Windows (`python -m app`). Nic nie działa na NAS-ie: kolejka, wykonywanie zadań i UI są w jednym procesie, a NAS jest tylko dyskiem z nagraniami. Opisy podziału na serwer na NAS-ie i worker na PC poniżej są nieaktualne.

- **Sklejanie (stitching):** Insta360 Media SDK. Wniosek o dostęp do SDK jest złożony.
- SDK to biblioteka C++ wymagająca dedykowanej karty graficznej, więc sklejanie odbywa się na PC, nie na NAS-ie. WSL nie jest wspierany, zakładamy natywny build pod Windows.
- **Worker:** PC z RTX 4080 albo starszy PC z RTX 3070 Ti. Ta sama binarka i ten sam skrypt, maszynę wskazuje konfiguracja.
- Propozycja startowa: 3070 Ti jako stały worker, test na jednym klipie 8K (8 GB VRAM może być za mało); w razie problemów przełączenie na 4080 bez zmian w kodzie.
- **NAS:** magazyn plików źródłowych i gotowych filmów, kolejka zadań.
- **Stitcher jako osobne CLI** (C++ na Media SDK): INSV na wejściu, ustabilizowany equirectangular MP4 na wyjściu. Reszta aplikacji (Python + FFmpeg) nie zależy od SDK.
- Kodowanie przez NVENC.

## Pipeline modułu 1

1. **Ingest:** wykrycie nowych plików; złożenie nagrania podzielonego przez kamerę na kilka plików w jedną oś czasu.
2. **Analiza na danych i proxy:** GPS, IGC, żyroskop, dźwięk, obraz w niskiej rozdzielczości. Bez sklejania pełnej jakości.
3. **Lista scen z oceną:** kandydaci do filmu.
4. **Plan kadru:** dla każdej sceny punkty czas → yaw / pitch / FOV, zapisane w JSON.
5. **Stitch tylko wybranych fragmentów** w pełnej jakości; wynik trafia do cache.
6. **Render:** FFmpeg (equirectangular → płaski kadr) według planu, z wygładzonym ruchem wirtualnej kamery.
7. **Łączenie** ujęć w gotowy film.

Plan kadru w JSON jest kontraktem między modułami: można go poprawić ręcznie przed renderem, a moduł 2 dostaje gotowy film i oś czasu.

## Konfiguracja

Jeden plik YAML na profil:

- długość docelowa i format (9:16 albo 16:9),
- wagi typów scen oraz minimalna i maksymalna długość ujęcia,
- presety kadru (kąty, pole widzenia),
- tryb horyzontu na preset,
- szybkość przejść między kadrami.

**Długość filmu jest konfigurowalna** i działa jak budżet rozdzielany między sceny. Można ją nadpisać przy pojedynczym uruchomieniu; nowa wersja powstaje bez ponownej analizy i sklejania.

## Profil „lot”

**Montaż kamery:** w kadłubie, zwykle na tablicy przyrządów przed pilotem. Kamera stoi w osi kadłuba, więc kalibracja to jednorazowe wskazanie przodu, a presety są stałymi kątami.

**Presety kadru:**

- **Przód:** czysty widok przez owiewkę. Start, hol, lądowanie.
- **Pilot:** zbliżenie na wprost.
- **Pilot szeroko:** pilot i oba skrzydła za nim. Kadr domyślny.
- **Skos w tył, lewy i prawy:** skrzydło i ziemia; w krążeniu wybierane jest skrzydło wewnętrzne.
- Przejście: płynny obrót między „pilot” a „przód”.

Kadr na przyrządy odpada (kamera ma je pod sobą); dane lotu jako grafika to temat modułu 2.

**Horyzont:** związany z szybowcem dla ujęć z wnętrzem kabiny, zablokowany tylko dla czystych widoków na zewnątrz.

**Ekspozycja:** osobna korekta jasności na preset (wnętrze kabiny kontra widok za owiewką).

**Nagranie:** jeden ciągły plik z całego lotu. Przy długich lotach nagranie może się skończyć przed lądowaniem.

**Źródła danych, w kolejności jakości:**

1. GPS z pilota GPS zapisany w pliku kamery (używany zazwyczaj, nie zawsze), synchronizacja z IGC po czasie GPS.
2. Samo IGC z SeeYou Navigator (dostępne zazwyczaj), dopasowanie przez korelację zakrętów z żyroskopem.
3. Tryb awaryjny: sam żyroskop i dźwięk.

**Sceny wykrywane z IGC:** start, koniec wznoszenia (wyczepienie lub wyłączenie silnika, w motoszybowcu także po dźwięku), krążenie w kominach z kierunkiem i noszeniem, przeloty, krąg i lądowanie.

**Scenariusz:** kolejność chronologiczna. Aplikacja porównuje koniec nagrania z IGC i wie, których faz brakuje; przy braku lądowania rolka dostaje inne zakończenie (np. najlepszy komin albo szeroki kadr z pilotem).

**Kilka rolek z jednego lotu:** nagranie może trwać do dwóch godzin, więc z jednej listy scen powstaje kilka rolek. Podział jest tematyczny.

- Temat to szablon w konfiguracji: typy scen, ich kolejność i kadry. Można dopisywać własne.
- Zestaw startowy: start i hol, komin, przelot i widoki, lądowanie oraz skrót lotu. Temat „lądowanie” jest domyślnie wyłączony i włączany świadomie dla danego zadania; przy wyłączonym skrót lotu także nie używa scen lądowania i kończy się inaczej.
- Każda scena trafia domyślnie do jednej rolki; tylko skrót lotu pożycza sceny z pozostałych.
- Rolka powstaje tylko przy wystarczającej ilości dobrego materiału (np. brak nagranego lądowania oznacza brak tej rolki).
- Liczba rolek proponowana po analizie albo podana ręcznie; każda rolka ma własną długość.
- W przeglądzie scen ujęcia są pogrupowane według rolek i można je przenosić między nimi.

**Na później:** temat „akrobacja” (możliwie w przyszłym roku). Wykrywanie figur z żyroskopu i akcelerometru kamery; te dane są zbierane od początku, więc dojdzie tylko nowy detektor i szablon.

**Warianty montażu:** montaż to osobny zestaw presetów i własna kalibracja, wybierany przy tworzeniu zadania.

- **Szybowiec, tablica przyrządów:** presety opisane wyżej.
- **Motoszybowiec, owiewka (mniej więcej na środku):** widać tablicę przyrządów, więc dochodzi preset „przyrządy”; miejsca zwykle obok siebie, więc presety na załogę to „lewy fotel”, „prawy fotel” i szerokie „załoga” z obiema osobami, z przełącznikiem, czy drugą osobę pokazywać. Ujęcia do przodu preferowane przy wyłączonym silniku (śmigło w kadrze). Sceny: start na silniku, wznoszenie, wyłączenie silnika zamiast holu i wyczepienia. Dokładne kąty do ustalenia po obejrzeniu przykładowego nagrania.

## Profil „urlop”

**Materiał:** spacery z kijem do selfie w ręku, kamera 360 na końcu kija.

**Informacje bez analizy obrazu:**

- położenie trzymającego wynika z kierunku kija i czujnika grawitacji,
- ujęcia do odrzucenia: kamera opuszczona, kij w dół, pierwsze i ostatnie sekundy klipu,
- kierunek marszu z GPS (gdy jest pilot) albo z ruchu obrazu na proxy.

**Kadry:**

- **Widok:** w kierunku marszu. Kadr domyślny.
- **Trzymający:** kadr na osobę z kijem, z tłem za plecami.
- **Towarzysz:** rozpoznana osoba idąca obok, łagodnie śledzona.
- **Panorama:** powolny obrót przy postoju, np. na punkcie widokowym.

**Horyzont:** zawsze zablokowany.

**Tempo:** długie przejścia przyspieszane, normalna prędkość tam, gdzie coś się dzieje (mowa, postój, widok).

**Wybór fragmentów:** ruch z żyroskopu, dźwięk, obecność rozpoznanych osób.

**Tryby wyjścia:**

- **Dzień:** jeden film na każdy dzień, chronologicznie.
- **Wyjazd:** jeden długi film z całego urlopu, dni jako rozdziały; budżet czasu dzielony proporcjonalnie do ilości dobrego materiału, z dolnym limitem na dzień.

Podział na dni po dacie nagrania w czasie lokalnym, z granicą doby przesuniętą na wczesny ranek. Analiza i sklejanie robią się raz dla obu trybów.

## Profil „inne”

Profil bez predefiniowanych reguł: wszystkie ustawienia wybiera się przy tworzeniu zadania, pod konkretny materiał.

- **Format i długość:** 9:16, 16:9 albo 1:1.
- **Kadrowanie:** stały kierunek, kierunek ruchu, śledzenie ludzi albo własne presety kątów.
- **Horyzont:** zablokowany albo związany z kamerą.
- **Wybór scen:** które sygnały się liczą (ruch, dźwięk, osoby, GPS, opcjonalnie IGC) i z jaką wagą.
- **Tempo i dźwięk:** przyspieszanie długich fragmentów; dźwięk oryginalny albo ujęcia nieme.
- **Wynik:** jeden film, film na dzień, kilka rolek albo same ujęcia.

Profile „lot” i „urlop” są zapisanymi zestawami tych samych ustawień, więc silnik jest jeden. Zestaw z profilu „inne” można zapisać pod własną nazwą jako nowy profil i użyć ponownie.

## Rozpoznawanie osób (Immich)

- Zwykle w wyjazdach biorą udział dwie do czterech osób; aplikacja ma je rozpoznawać.
- Osoby są już podpisane imionami w Immichu. Wykorzystujemy istniejące wektory twarzy z Immicha, bez trenowania własnego modelu.
- Jednorazowy eksport wektorów wybranych osób do własnego pliku aplikacji, odświeżany na żądanie; baza Immicha tylko do odczytu.
- Ten sam model rozpoznawania co w Immichu, inaczej wektory są nieporównywalne.
- Detekcja twarzy na płaskich widokach wyciętych z proxy, nie na surowym obrazie sferycznym.
- Rozpoznanie łączone ze śledzeniem sylwetki (okulary, czapki).

**Reguły kadrowania:** znane osoby mają priorytet, obcy nie są śledzeni; kadr grupowy, gdy kilka osób jest blisko siebie; wyrównywanie czasu ekranowego między osobami, z wagami w konfiguracji.

## Uruchamianie i UI

Zadanie to wskazany folder na NAS-ie z nagraniami; aplikacja obrabia jego zawartość.

- **Profil:** wybierany przy uruchomieniu; domyślnie zgadywany (plik IGC w folderze oznacza lot, brak oznacza urlop).
- **Wynik:** podfolder z gotowymi filmami w tym samym miejscu, obok plan kadru w JSON i raport z wyboru scen.
- **Oryginały:** tylko do odczytu, nic nie jest przenoszone ani kasowane.
- **Dysk roboczy:** worker kopiuje potrzebne pliki na lokalny szybki dysk; proxy i cache lokalnie, na NAS wraca wynik.
- **Wznawianie:** stan zadania zapisany w folderze; ponowne wskazanie folderu przerabia tylko nowe pliki.

**Proste UI w przeglądarce, cztery ekrany:**

- **Nowe zadanie:** folder, profil, długość, tryb „dzień” lub „wyjazd”, lista osób z Immicha.
- **Kolejka:** zadania z postępem i etapem (analiza, sklejanie, render).
- **Wyniki:** podgląd, pobranie, nowa wersja ze zmienioną długością bez ponownej analizy.
- **Ustawienia:** edycja profili (presety, wagi scen, długości ujęć).

Panel i kolejka działają w kontenerze na NAS-ie; worker na PC zabiera zadania, gdy jest włączony. Dostęp spoza domu przez Tailscale. Propozycja stosu: FastAPI, SQLite, lekki frontend.

**Opcjonalny przegląd scen (przełącznik przy tworzeniu zadania):**

- Zadanie zatrzymuje się po analizie, przed sklejaniem w pełnej jakości.
- Lista scen w kolejności filmu: miniatura z proxy, typ, kadr, długość, ocena.
- Akcje na scenie: odrzuć, zmień preset kadru, wydłuż lub skróć; można dołożyć scenę z listy niewykorzystanych.
- Długość filmu przeliczana na bieżąco; „Renderuj” uruchamia resztę.

**Pobieranie samych ujęć (bez łączenia w film):**

- Każda scena renderowana jako osobny plik w docelowym kadrze, ponumerowany w kolejności filmu.
- W wynikach dwa warianty z tych samych ujęć: gotowy film albo same ujęcia (paczka lub pojedynczo), np. do montażu w aplikacji TikTok.
- Zapas sekundy lub dwóch na obu końcach ujęcia do przycinania w aplikacji.
- Kodowanie ujęć w H.264 dla zgodności z aplikacjami w telefonie.

## Dźwięk

- **Lot:** ujęcia domyślnie nieme; przełącznik w konfiguracji. Dźwięk nadal służy do analizy (np. wyłączenie silnika w motoszybowcu).
- **Urlop:** oryginalny dźwięk zostaje.
- W przyspieszonych fragmentach dźwięk jest wyciszany albo zastępowany tłem w normalnym tempie.
- Krótkie przenikanie dźwięku na łączeniach ujęć.

## Materiał źródłowy i publikacja

- **Lot:** zwykle 5.7K, pozostałe ustawienia kamery domyślne. Szerokie kadry wyglądają dobrze, ciasne zbliżenia będą miększe niż z 8K. Do sprawdzenia: jeden krótki lot w 8K dla porównania. Aplikacja obsługuje obie rozdzielczości.
- **Urlop:** zwykle 8K, ustawienia domyślne. Wyjście 16:9 w 1080p.
- **Kolor:** standardowy profil kamery, więc tylko lekka, spójna korekta na profil.
- **Publikacja rolek z lotów:** zwykle TikTok, czasem Instagram. Kadrowanie trzyma pilota i horyzont poza strefami zasłanianymi przez interfejs (dół i prawy brzeg); jedna wspólna strefa bezpieczna dla obu platform, parametr w profilu, podgląd stref w przeglądzie scen.
- **Filmy z urlopu:** zwykle niepublikowane, bez rozmywania twarzy. Rozpoznawanie osób służy tylko do kadrowania.
- **Kod:** aplikację pisze Claude Code; dokument zostanie przerobiony na specyfikację z etapami i kryteriami ukończenia.

## Przechowywanie

- **Oryginały:** wszystkie surowe nagrania zostają na NAS-ie na stałe (21 TB wolnego miejsca, stan na listopad 2026). Aplikacja ich nie rusza.
- **Plan kadru i lista scen (JSON):** trzymane na stałe obok wyniku; z oryginału i planu każdy film da się odtworzyć.
- **Gotowe filmy i ujęcia:** na NAS-ie, w podfolderze zadania.
- **Proxy:** małe, trzymane, żeby przegląd scen i nowe wersje działały od ręki.
- **Cache sklejonych fragmentów:** duży, na lokalnym dysku workera, z limitem rozmiaru i automatycznym usuwaniem najdawniej używanych.

## Etap 1

Najkrótsza droga do działającego filmu, uzgodniona jako start:

- jeden lot, ręcznie wyeksportowany z Insta360 Studio jako equirectangular,
- plik IGC, wykrywanie scen, presety kadru, render rolki,
- bez Media SDK, bez UI, bez rozpoznawania twarzy.

Cel: sprawdzić logikę scen i kadrów, zanim przyjdzie dostęp do SDK.

## Otwarte kwestie

- Dostęp do Media SDK i potwierdzenie, czy przyjmuje zakres czasu przy sklejaniu.
- Test wydajności i pamięci na RTX 3070 Ti dla materiału 8K.
- Sposób odczytu GPS z pliku kamery.
- Szczegóły UI: sposób wyboru folderu z NAS-a w przeglądarce i wygląd ekranu przeglądu scen.
- Konkretne wartości domyślne: długości ujęć, wagi scen, kąty presetów.

* Przejścia między ujęciami: decyzja odłożona do pierwszego gotowego filmu. Wartość domyślna: twarde cięcia między scenami, płynny obrót kamery tylko wewnątrz sceny; parametr w profilu.

## Specyfikacja dla Claude Code

Sekcje powyżej są wymaganiami; ta część ustala sposób i kolejność budowy. Każdy etap kończy się działającym, sprawdzalnym wynikiem. Następny etap zaczyna się dopiero po spełnieniu kryteriów poprzedniego.

### Zasady ogólne

- **Struktura repozytorium:**
  - `core/` – pakiet Pythona: ingest, analiza, sceny, plan kadru, render, montaż,
  - `stitcher/` – CLI w C++ na Media SDK (etap 4),
  - `worker/` – wykonywanie zadań na PC,
  - `server/` – API, kolejka i UI,
  - `profiles/` – profile, montaże kamery i tematy w YAML,
  - `tests/` – testy i małe dane przykładowe.
- **Język:** Python dla całej logiki; FFmpeg wywoływany jako proces, każde polecenie zapisywane w logu. C++ tylko w stitcherze.
- **Kontrakty danych:** `scenes.json`, `reel_plan.json`, `framing_plan.json`, `job_state.json`, każdy ze schematem i walidacją. Moduły komunikują się wyłącznie przez te pliki.
- **Kroki pipeline'u:** każdy krok to osobne polecenie CLI, idempotentne, z cache zależnym od wejścia i parametrów. UI tylko wywołuje te same kroki.
- **Stitcher za interfejsem** z dwiema implementacjami: gotowy plik equirectangular z Insta360 Studio oraz CLI na Media SDK. Reszta kodu nie wie, która działa.
- **Konfiguracja:** żadnych kątów, długości ani wag wpisanych w kod; wszystko w `profiles/`.
- **Oryginały tylko do odczytu.** Aplikacja pisze wyłącznie do podfolderu zadania i do lokalnego cache.
- **Platforma:** `core/` działa na Windows i Linuksie; worker natywnie na Windows.
- **Testy:** testy jednostkowe detekcji scen na przykładowych plikach IGC, testy porównujące wygenerowane plany ze wzorcem, krótki klip equirectangular do testu renderu.
- **Ukończenie etapu:** testy przechodzą, README opisuje uruchomienie, w repozytorium jest zapis przykładowego przebiegu.

### Etap 1 – rolka z jednego lotu

Bez SDK, bez UI, bez rozpoznawania twarzy.

- **Wejście:** plik equirectangular wyeksportowany ręcznie z Insta360 Studio i plik IGC z tego samego lotu.
- **Zakres:**
  - parser IGC i detekcja faz: start, koniec wznoszenia, kominy z kierunkiem i noszeniem, przeloty, krąg i lądowanie,
  - synchronizacja wideo z IGC przez ręcznie podane przesunięcie czasu,
  - montaż kamery „szybowiec, tablica”: presety przód, pilot, pilot szeroko, skos w tył lewy i prawy; kalibracja jednym kątem,
  - generator planu kadru z wygładzaniem ruchu i obrotem między presetami wewnątrz sceny,
  - render do 1080×1920 przez FFmpeg, twarde cięcia, łączenie w jeden plik,
  - jedna rolka „skrót lotu” o konfigurowalnej długości, bez dźwięku, bez scen lądowania.
- **Kryteria ukończenia:**
  - jedno polecenie z folderem na wejściu daje gotową rolkę,
  - zmiana długości tworzy nową wersję bez ponownej analizy,
  - wykryte fazy lotu zgadzają się z ręcznym opisem lotu testowego z dokładnością do kilku sekund,
  - w krążeniu wybierane jest skrzydło wewnętrzne.

### Etap 2 – tematy, kilka rolek, osobne ujęcia

- **Zakres:**
  - warstwa planu rolek: tematy jako szablony (start i hol, komin, przelot i widoki, lądowanie, skrót lotu),
  - scena domyślnie w jednej rolce; skrót lotu może pożyczać,
  - próg ilości materiału, poniżej którego rolka nie powstaje,
  - temat „lądowanie” domyślnie wyłączony,
  - eksport samych ujęć: osobne pliki H.264, numerowane, z zapasem na końcach,
  - strefa bezpieczna dla TikToka i Instagrama uwzględniana w kadrowaniu,
  - tryb horyzontu i korekta jasności na preset.
- **Kryteria ukończenia:**
  - z lotu testowego powstaje kilka rolek bez powtarzających się scen,
  - przy wyłączonym lądowaniu żadna rolka nie zawiera scen kręgu ani przyziemienia,
  - paczka ujęć odpowiada scenom rolki w tej samej kolejności,
  - własny temat da się dodać samym plikiem YAML.

### Etap 3 – dane z kamery i automatyczna synchronizacja

- **Zakres:**
  - odczyt żyroskopu, akcelerometru i GPS z pliku kamery,
  - automatyczna synchronizacja: po czasie GPS, a bez GPS przez korelację zakrętów z IGC z żyroskopem,
  - tryb awaryjny bez IGC: sceny z samego żyroskopu i dźwięku,
  - składanie nagrania podzielonego na kilka plików w jedną oś czasu,
  - wykrywanie, że nagranie skończyło się przed lądowaniem, i dobór zakończenia,
  - montaż kamery „motoszybowiec, owiewka”: presety przyrządy, lewy fotel, prawy fotel, załoga; przełącznik pokazywania drugiej osoby,
  - sceny motoszybowca: start na silniku, wznoszenie, wyłączenie silnika wykrywane po dźwięku.
- **Kryteria ukończenia:**
  - synchronizacja bez ręcznego przesunięcia, z błędem poniżej pół sekundy na lotach testowych,
  - lot bez pliku IGC nadal daje rolkę,
  - lot urwany przed lądowaniem daje rolki z poprawnym zakończeniem.

### Etap 4 – stitcher na Media SDK

Wymaga przyznanego dostępu do SDK.

- **Zakres:**
  - CLI w C++: pliki kamery na wejściu, equirectangular MP4 na wyjściu, dla wskazanego zakresu czasu,
  - generowanie proxy w niskiej rozdzielczości dla całego nagrania,
  - sklejanie w pełnej jakości tylko fragmentów wybranych w planie,
  - sterowanie stabilizacją tak, żeby tryb horyzontu (zablokowany albo związany z płatowcem) dało się wybrać na etapie kadrowania,
  - cache sklejonych fragmentów z limitem rozmiaru i usuwaniem najdawniej używanych,
  - kopiowanie plików źródłowych z NAS-a na lokalny dysk roboczy,
  - pomiar czasu i zużycia pamięci karty dla 5.7K i 8K na RTX 3070 Ti i RTX 4080.
- **Kryteria ukończenia:**
  - od surowych plików kamery do rolki bez ręcznego kroku w Insta360 Studio,
  - dwugodzinne nagranie przechodzi bez sklejania całości w pełnej jakości,
  - wyniki pomiarów zapisane w repozytorium wraz z zaleceniem, która maszyna jest workerem.

### Etap 5 – profil „urlop”

- **Zakres:**
  - grupowanie klipów w dni po czasie lokalnym, z przesuniętą granicą doby,
  - odrzucanie ujęć po czujniku grawitacji (kamera opuszczona, kij w dół) i przycinanie początku i końca klipu,
  - położenie trzymającego z kierunku kija; kierunek marszu z GPS albo z ruchu obrazu,
  - detekcja i śledzenie ludzi na płaskich widokach z proxy,
  - kadry: widok, trzymający, towarzysz, panorama; horyzont zablokowany,
  - przyspieszanie długich przejść i powrót do normalnego tempa tam, gdzie coś się dzieje,
  - dźwięk oryginalny, wyciszany w przyspieszeniach, z przenikaniem na cięciach,
  - wyjście 16:9 w 1080p; tryby „dzień” i „wyjazd” z jednej analizy.
- **Kryteria ukończenia:**
  - folder z kilkudniowym materiałem daje po jednym filmie na dzień oraz, na żądanie, jeden film z całości,
  - film z wyjazdu powstaje bez ponownej analizy i sklejania,
  - w gotowym filmie nie ma ujęć z opuszczoną kamerą.

### Etap 6 – rozpoznawanie osób z Immicha

- **Zakres:**
  - eksport wektorów twarzy wskazanych osób z Immicha, tylko do odczytu, do własnego pliku aplikacji,
  - ten sam model rozpoznawania co w Immichu,
  - dopasowanie twarzy z klatek do osób i powiązanie ze śledzoną sylwetką,
  - reguły: priorytet znanych osób, brak śledzenia obcych, kadr grupowy, wyrównywanie czasu ekranowego,
  - obecność znanej osoby podnosi ocenę sceny.
- **Kryteria ukończenia:**
  - na materiale testowym osoby z listy są rozpoznawane, a osoby spoza listy nigdy nie są celem kadru,
  - aktualizacja Immicha nie psuje pipeline'u; w najgorszym razie nie działa tylko ponowny eksport.

### Etap 7 – kolejka, worker i UI

- **Zakres:**
  - serwer w kontenerze na NAS-ie: API, kolejka w SQLite, UI w przeglądarce,
  - worker na PC pobierający zadania, gdy jest włączony; wznawianie przerwanych zadań,
  - ekrany: nowe zadanie, kolejka, wyniki, ustawienia,
  - wybór folderu z NAS-a, profilu, montażu kamery, długości, tematów i osób,
  - opcjonalny przegląd scen: zatrzymanie po analizie, miniatury z proxy, odrzucanie, zmiana presetu, zmiana długości, przenoszenie scen między rolkami, nakładka strefy bezpiecznej,
  - pobieranie gotowego filmu albo samych ujęć,
  - dostęp przez Tailscale, bez wystawiania do internetu.
- **Kryteria ukończenia:**
  - zadanie zlecone z telefonu wykonuje się po włączeniu PC,
  - ponowne wskazanie tego samego folderu przerabia tylko nowe pliki,
  - wszystko, co robi UI, da się zrobić tymi samymi poleceniami CLI.

**Profil „inne” w etapach:** od etapu 2 każdy profil jest zestawem tych samych ustawień w YAML, więc zadanie z własnym zestawem działa z linii poleceń. Etap 7 dodaje formularz wszystkich ustawień przy tworzeniu zadania i zapis zestawu jako nowego profilu. Kryterium: zadanie z profilem „inne” daje film bez żadnej zmiany w kodzie.

### Później

- Temat „akrobacja”: detektor figur z żyroskopu i akcelerometru oraz nowy szablon.
- Moduł 2: napisy, muzyka, nakładki z danymi lotu.

### Dane testowe do przygotowania

- Jeden lot szybowcem: plik z kamery, eksport equirectangular z Insta360 Studio i plik IGC.
- Jeden lot motoszybowcem z montażem na owiewce.
- Jeden lot bez IGC i jeden urwany przed lądowaniem.
- Jeden dzień urlopu z kilkoma klipami i osobami podpisanymi w Immichu.
