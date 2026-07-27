"""Curated attraction, restaurant, and transit tips for day cards."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class DayIdea:
    title: str
    go: str  # main place(s) to visit
    also: str  # second stop
    lunch: str
    dinner: str
    route: str  # how to get there (train / metro / walk)


# Named sights, meals, and transit the UI can show when the LLM is vague.
_GUIDES: dict[str, list[DayIdea]] = {
    "tokyo": [
        DayIdea(
            title="Arrival · Shinjuku",
            go="Shinjuku Station -> hotel check-in, then Shinjuku Gyoen",
            also="Tokyo Metropolitan Government Building observatory (free night view)",
            lunch="Ichiran Ramen (Shinjuku)",
            dinner="Omoide Yokocho yakitori alley (west exit of Shinjuku Station)",
            route="Airport Limousine Bus or Narita Express / Keikyu to Shinjuku Station; walk 5-10 min in Shinjuku",
        ),
        DayIdea(
            title="Shibuya & Harajuku",
            go="Meiji Jingu shrine, then Takeshita Street (Harajuku)",
            also="Shibuya Scramble Crossing & Shibuya Sky (book timed entry)",
            lunch="Harajuku Gyoza Lou (Takeshita-dori side street)",
            dinner="Uobei Shibuya (sushi) near Scramble Crossing",
            route="JR Yamanote Line: Shinjuku -> Harajuku (1 stop); walk to Meiji Jingu. Then JR Harajuku -> Shibuya (1 stop) or walk 15 min via Omotesando",
        ),
        DayIdea(
            title="Asakusa & Skytree",
            go="Senso-ji Temple + Nakamise shopping street (Asakusa)",
            also="Tokyo Skytree observation decks",
            lunch="Asakusa Okonomiyaki Sometaro",
            dinner="Gyukatsu Motomura (Akihabara) after evening stroll",
            route="Tokyo Metro Ginza Line to Asakusa Stn (exit 1 for Senso-ji). Walk ~15 min or Tobu Skytree Line 1 stop to Tokyo Skytree. Metro Ginza Line Asakusa -> Suehirocho/Akihabara for dinner",
        ),
        DayIdea(
            title="Ginza & Imperial Palace",
            go="Imperial Palace East Gardens (Otemachi / Nijubashimae)",
            also="Ginza Chuo-dori & Ginza Six depachika",
            lunch="Conveyor sushi near Ginza Six / Mitsukoshi area",
            dinner="Yakiniku Champion Ginza",
            route="Tokyo Metro Chiyoda / Tozai / Mita lines to Nijubashimae or Otemachi; walk the gardens. Then Metro Ginza Line or Marunouchi Line to Ginza Stn",
        ),
        DayIdea(
            title="Odaiba / teamLab",
            go="teamLab Planets (Toyosu) or teamLab Borderless - book ahead",
            also="DiverCity Tokyo Plaza (Unicorn Gundam) on Odaiba",
            lunch="Food floor inside DiverCity / Aqua City",
            dinner="Izakaya near your hotel (ask front desk for a nearby pick)",
            route="Yurikamome Line from Shimbashi -> Daiba / Odaiba-kaihinkoen; or Rinkai Line to Tokyo Teleport. For teamLab Planets: Yurikamome to Telecom Center / Tokyo Metro to Toyosu + walk",
        ),
        DayIdea(
            title="Day trip · Kamakura",
            go="Kotokuin (Great Buddha) and Hasedera Temple, Kamakura",
            also="Komachi-dori shopping street back near Kamakura Station",
            lunch="Cafe or vegan-friendly lunch on Komachi-dori",
            dinner="Conveyor-belt sushi near Tokyo station after return",
            route="JR Yokosuka Line or Shonan-Shinjuku Line to Kamakura (~1 hr from Tokyo/Shinjuku). Enoden tram Kamakura -> Hase for Great Buddha. Return same JR line",
        ),
        DayIdea(
            title="Departure",
            go="Last souvenir stop - Don Quijote or station depachika",
            also="Hotel checkout -> airport (HND or NRT)",
            lunch="Station bento or airport ramen",
            dinner="Light snack only on the plane / airside",
            route="To Haneda: Tokyo Monorail or Keikyu Airport Line. To Narita: Narita Express (N'EX) or Keisei Skyliner. Leave 3+ hrs before flight",
        ),
    ],
    "seoul": [
        DayIdea(
            title="Arrival · Palace area",
            go="Gyeongbokgung Palace + Bukchon Hanok Village",
            also="Insadong craft streets & Jogyesa Temple",
            lunch="Tosokchon Samgyetang (ginseng chicken)",
            dinner="Gwangjang Market food hall",
            route="AREX or limousine bus from ICN to Seoul Station / hotel, then Metro Line 3 to Gyeongbokgung. Walk Bukchon -> Anguk (Line 3) for Insadong",
        ),
        DayIdea(
            title="Hongdae & Namsan",
            go="Hongdae streets + Yeonnam-dong cafes",
            also="N Seoul Tower (Namsan cable car)",
            lunch="Hongdae tteokbokki stalls or Seongsimdang bakery",
            dinner="Myeongdong K-BBQ / hotteok alleys",
            route="Metro Line 2 to Hongik Univ. Then Line 2/3/4 toward Myeongdong; walk to Namsan cable car base (or bus 02)",
        ),
        DayIdea(
            title="Gangnam & Seongsu",
            go="Starfield COEX Mall + Bongeunsa Temple",
            also="Seoul Forest + Seongsu-dong cafes",
            lunch="Gangnam bibimbap set near COEX",
            dinner="Samgyeopsal BBQ in Gangnam or Seongsu",
            route="Metro Line 2 to Samseong (COEX). Line 2 to Ttukseom / Seongsu for Seoul Forest",
        ),
        DayIdea(
            title="Day trip option",
            go="DMZ tour (book ahead) OR Bukhansan hike",
            also="Ikseon-dong hanok alleys after return",
            lunch="Kimbap / noodles near tour meeting point",
            dinner="Chimaek (fried chicken & beer) in Ikseon-dong",
            route="DMZ: tour bus from central Seoul (pick-up listed on voucher). Bukhansan: Metro Line 3 to Gupabal + local bus. Return Metro to Jongno for Ikseon-dong",
        ),
        DayIdea(
            title="Departure",
            go="Last Myeongdong beauty / snack haul",
            also="Hotel -> Incheon Airport",
            lunch="Airport Korean meal before security",
            dinner="Light snack airside",
            route="AREX All-Stop or Express from Seoul Station to ICN; or airport limousine bus from hotel district. Leave 3.5+ hrs before flight",
        ),
    ],
    "osaka": [
        DayIdea(
            title="Arrival · Dotonbori",
            go="Dotonbori canal & Glico sign",
            also="Shinsaibashi shopping arcade",
            lunch="Kuromon Market seafood skewers",
            dinner="Takoyaki + okonomiyaki on Dotonbori",
            route="Airport limousine / Nankai or JR to Namba. Walk to Dotonbori (5 min from Namba Stn). Kuromon is a short walk east of Namba",
        ),
        DayIdea(
            title="Osaka Castle & Umeda",
            go="Osaka Castle Tower & park",
            also="Umeda Sky Building Floating Garden",
            lunch="Okonomiyaki in Fukushima / Umeda",
            dinner="Kushikatsu Daruma (Shinsekai or Namba branch)",
            route="JR Loop / Osaka Metro Tanimachi Line to Osakajokoen / Tanimachi 4-chome. Then JR/Metro to Umeda (Osaka Stn)",
        ),
        DayIdea(
            title="Nara day trip",
            go="Todai-ji Great Buddha + Nara Park deer",
            also="Kasuga Taisha shrine",
            lunch="Mochi + teishoku near Nara Park",
            dinner="Ramen near Namba after return",
            route="JR Yamatoji Rapid or Kintetsu Limited Express Osaka-Namba -> Nara (~45-50 min). Walk from Kintetsu-Nara / JR Nara to Todai-ji",
        ),
        DayIdea(
            title="Departure",
            go="Last snack / souvenir at station depachika",
            also="Hotel -> KIX or ITM",
            lunch="Airport okonomiyaki or ramen",
            dinner="Light snack airside",
            route="Nankai or JR Haruka to Kansai Airport (KIX); or monorail/bus to Itami (ITM). Leave 3+ hrs before flight",
        ),
    ],
    "paris": [
        DayIdea(
            title="Arrival · Île de la Cité",
            go="Notre-Dame exterior + Sainte-Chapelle (book timed entry)",
            also="Seine riverside walk toward Musée d'Orsay",
            lunch="Left Bank café near Saint-Michel",
            dinner="Classic bistrot in the 5th / 6th",
            route="RER B from CDG to Saint-Michel-Notre-Dame or Metro Line 4. Sainte-Chapelle is at Boulevard du Palais - walk 5 min from Cité Metro (Line 4)",
        ),
        DayIdea(
            title="Louvre & Opéra",
            go="Louvre Museum (prebook; pick 2-3 wings)",
            also="Galeries Lafayette dome + Opéra Garnier exterior",
            lunch="Angelina (Rue de Rivoli) - hot chocolate",
            dinner="Marais wine bar / small plates",
            route="Metro Line 1 or 7 to Palais Royal-Musée du Louvre. Then Metro Line 7/8/9 toward Opéra / Chaussée d'Antin",
        ),
        DayIdea(
            title="Eiffel & Champs",
            go="Eiffel Tower or Trocadéro viewpoint",
            also="Arc de Triomphe & Champs-Élysées",
            lunch="Crepes near Trocadéro / Champ de Mars",
            dinner="Steak-frites bistrot in the 7th",
            route="RER C or Metro Line 6/9 to Trocadéro / Bir-Hakeim. Metro Line 1 to Charles de Gaulle-Étoile for the Arc",
        ),
        DayIdea(
            title="Montmartre & Marais",
            go="Sacré-Cœur & Place du Tertre (early)",
            also="Place des Vosges + Marais boutiques",
            lunch="Bouillon Pigalle",
            dinner="L'As du Fallafel (Rue des Rosiers)",
            route="Metro Line 2 to Anvers + funicular/walk up; or Line 12 to Abbesses. Then Metro to Saint-Paul (Line 1) for the Marais",
        ),
        DayIdea(
            title="Departure",
            go="Last bakery / souvenir stop",
            also="Hotel -> CDG or ORY",
            lunch="Airport café meal",
            dinner="Light snack airside",
            route="RER B to CDG; Orlyval + RER B / Metro 7 for ORY. Leave 3+ hrs before flight",
        ),
    ],
    "bangkok": [
        DayIdea(
            title="Arrival · Riverside",
            go="Wat Arun at golden hour",
            also="Chao Phraya river ferry hop",
            lunch="Thip Samai pad thai",
            dinner="Riverside seafood or Yaowarat if energy allows",
            route="Airport Rail Link to Phaya Thai + BTS, or taxi to hotel. BTS to Saphan Taksin -> Chao Phraya Express Boat to Wat Arun pier",
        ),
        DayIdea(
            title="Grand Palace & Chinatown",
            go="Grand Palace & Wat Phra Kaew (dress code)",
            also="Wat Pho reclining Buddha",
            lunch="Rice-and-curry near Sanam Luang",
            dinner="Yaowarat (Chinatown) street-food crawl",
            route="River boat to Tha Chang pier for Grand Palace; walk to Wat Pho. Taxi/MRT to Wat Mangkon for Yaowarat evening",
        ),
        DayIdea(
            title="Modern Bangkok",
            go="Jim Thompson House",
            also="Siam Paragon / MBK shopping",
            lunch="Som Tam Nua (Siam)",
            dinner="Boat noodle alley or booked Thai restaurant",
            route="BTS to National Stadium (Jim Thompson walk). BTS Siam for malls",
        ),
        DayIdea(
            title="Departure",
            go="Last massage near hotel",
            also="Hotel -> Suvarnabhumi (BKK)",
            lunch="Airport Thai meal",
            dinner="Light snack airside",
            route="Airport Rail Link from Phaya Thai, or taxi (allow heavy traffic). Leave 3.5+ hrs before flight",
        ),
    ],
    "singapore": [
        DayIdea(
            title="Arrival · Marina Bay",
            go="Gardens by the Bay (Supertree Grove)",
            also="Marina Bay Sands SkyPark (ticketed) or waterfront walk",
            lunch="Maxwell Food Centre - chicken rice",
            dinner="Chilli crab (Jumbo Seafood - book ahead)",
            route="MRT East-West / Circle to Bayfront or Promenade. Walk Gardens. MRT to Maxwell / Tanjong Pagar for lunch",
        ),
        DayIdea(
            title="Culture triangle",
            go="National Gallery or Asian Civilisations Museum",
            also="Kampong Glam - Sultan Mosque & Haji Lane",
            lunch="Zam Zam murtabak (Kampong Glam)",
            dinner="Lau Pa Sat satay street",
            route="MRT to City Hall / Raffles Place. Then MRT Downtown Line to Bugis / walk to Kampong Glam. MRT to Telok Ayer / Raffles Place for Lau Pa Sat",
        ),
        DayIdea(
            title="Sentosa / Botanic",
            go="Singapore Botanic Gardens (UNESCO)",
            also="Sentosa beach OR Universal Studios (full-day choice)",
            lunch="Food court at Resorts World / Botanic area",
            dinner="Hawker dinner at Old Airport Road Food Centre",
            route="MRT Circle Line to Botanic Gardens. Sentosa: MRT HarbourFront + Sentosa Express monorail",
        ),
        DayIdea(
            title="Departure",
            go="Last bak kwa / pineapple tart run",
            also="Hotel -> Changi Airport",
            lunch="Jewel Changi or terminal food court",
            dinner="Light snack airside",
            route="MRT East-West Line to Tanah Merah + CG line to Changi; or taxi. Arrive early - Jewel is worth time",
        ),
    ],
    "taipei": [
        DayIdea(
            title="Arrival · Central Taipei",
            go="Chiang Kai-shek Memorial Hall",
            also="Ximending night streets",
            lunch="Din Tai Fung (soup dumplings) or Yongkang Street mango ice",
            dinner="Ningxia or Shilin Night Market",
            route="Airport MRT to Taipei Main Station; MRT Red Line to CKS Memorial Hall. Blue/Orange Line to Ximen for evening",
        ),
        DayIdea(
            title="Jiufen day trip",
            go="Jiufen Old Street",
            also="Return for Raohe Night Market",
            lunch="Taro balls + tea house snacks in Jiufen",
            dinner="Pepper buns at Raohe Night Market",
            route="Train from Taipei to Ruifang + local bus to Jiufen (~1.5 hr). MRT Green Line to Songshan for Raohe",
        ),
        DayIdea(
            title="Taipei 101 & Xiangshan",
            go="Taipei 101 observatory (timed ticket)",
            also="Elephant Mountain (Xiangshan) trail for skyline photos",
            lunch="Taipei 101 mall food court",
            dinner="Beef noodle shop near Yongchun / Sun Yat-sen Memorial",
            route="MRT Red Line to Taipei 101 / World Trade Center. Same station area walk to Xiangshan trailhead",
        ),
        DayIdea(
            title="Departure",
            go="Last pineapple cake / tea souvenirs",
            also="Hotel -> Taoyuan Airport (TPE)",
            lunch="Airport Taiwanese breakfast / brunch",
            dinner="Light snack airside",
            route="Airport MRT from Taipei Main Station to TPE Terminals. Leave 3+ hrs before flight",
        ),
    ],
    "hong kong": [
        DayIdea(
            title="Arrival · Harbour",
            go="Tsim Sha Tsui harbour promenade / Avenue of Stars",
            also="Star Ferry to Central",
            lunch="Cha chaan teng or Joy Hing char siu",
            dinner="Temple Street Night Market snacks",
            route="Airport Express to Kowloon / Hong Kong Stn + taxi/MTR. MTR Tsuen Wan Line to Tsim Sha Tsui. Star Ferry pier is a short walk",
        ),
        DayIdea(
            title="Peak & Central",
            go="Victoria Peak via Peak Tram (book/queue early)",
            also="Man Mo Temple + Sheung Wan streets",
            lunch="Dim sum in Central / Sheung Wan (e.g. Tim Ho Wan)",
            dinner="Kennedy Town dai pai dong-style dinner",
            route="MTR to Central -> Peak Tram lower terminus (Garden Road). MTR Island Line to Sheung Wan; Island Line to Kennedy Town for dinner",
        ),
        DayIdea(
            title="Lantau icons",
            go="Ngong Ping 360 + Big Buddha / Po Lin Monastery",
            also="Optional Tai O fishing village",
            lunch="Monastery vegetarian or Ngong Ping eateries",
            dinner="Kam's Roast Goose or roast-meat rice in Central/Wan Chai",
            route="MTR Tung Chung Line to Tung Chung -> Ngong Ping 360 cable car. Return same way; MTR back to Hong Kong Island for dinner",
        ),
        DayIdea(
            title="Departure",
            go="Last egg tart / pineapple bun run",
            also="Hotel -> Hong Kong International Airport",
            lunch="Airport noodle shop",
            dinner="Light snack airside",
            route="Airport Express from Hong Kong / Kowloon / Tsing Yi stations. Leave 3+ hrs before flight",
        ),
    ],
    "london": [
        DayIdea(
            title="Arrival · South Bank",
            go="London Eye / South Bank walk toward Borough Market",
            also="Tate Modern exterior + Millennium Bridge views",
            lunch="Borough Market (Padella pasta queue or market stalls)",
            dinner="Flat Iron (steak) Covent Garden or Dishoom Covent Garden",
            route="Elizabeth line / Heathrow Express to Paddington, then Tube to Waterloo or London Bridge. Walk South Bank between Eye, Tate Modern, and Borough",
        ),
        DayIdea(
            title="Westminster & Museums",
            go="Westminster Abbey exterior + Big Ben / Houses of Parliament photo stop",
            also="British Museum (Great Court) — pick 2–3 rooms",
            lunch="Pret or museum cafés near Russell Square; or Dishoom Kings Cross if headed north",
            dinner="The Ivy Asia / Covent Garden casual — or Bao Soho for a quicker dinner",
            route="Tube Jubilee / District / Circle to Westminster. Then Northern / Central / Piccadilly toward Tottenham Court Road / Russell Square for the British Museum",
        ),
        DayIdea(
            title="Royal parks & shopping",
            go="Buckingham Palace Changing of the Guard (check timetable) + St James's Park",
            also="Harrods Food Hall or Oxford Street / Regent Street browse",
            lunch="Sketch Gallery (booking) or Fortnum & Mason café",
            dinner="Gordon Ramsay Street Pizza or Blacklock Soho",
            route="Tube Victoria / Green Park for the palace. Piccadilly Line or walk to Knightsbridge (Harrods) or Oxford Circus",
        ),
        DayIdea(
            title="East End & skyline",
            go="Tower of London (Crown Jewels) or Tower Bridge walk",
            also="Sky Garden (free timed booking) or Borough riverside at dusk",
            lunch="Cafe Below (St Mary-le-Bow) or Poppies fish & chips Spitalfields",
            dinner="Duck & Waffle or Honest Burgers near Liverpool Street",
            route="Tube District / Circle to Tower Hill. Walk to Tower Bridge; for Sky Garden walk to Fenchurch Street / Monument",
        ),
        DayIdea(
            title="Departure",
            go="Last stop: Liberty London or Covent Garden Market souvenirs",
            also="Hotel checkout → Heathrow (LHR) or Gatwick (LGW)",
            lunch="Station meal at King's Cross / Paddington / Victoria",
            dinner="Light snack airside only",
            route="Heathrow: Elizabeth line or Heathrow Express from Paddington. Gatwick: Thameslink / Gatwick Express from Victoria or London Bridge. Leave 3+ hrs before flight",
        ),
    ],
    "san francisco": [
        DayIdea(
            title="Arrival · Embarcadero",
            go="Ferry Building Marketplace + Embarcadero waterfront walk",
            also="Exploratorium exterior / Pier 15 area if time allows",
            lunch="Hog Island Oyster Co. or El Porteño empanadas (Ferry Building)",
            dinner="Kokkari Estiatorio or Tadich Grill (Financial District)",
            route="BART from SFO to Embarcadero / Montgomery. Walk the Embarcadero; Muni F-Line streetcar for short hops",
        ),
        DayIdea(
            title="Golden Gate & Presidio",
            go="Golden Gate Bridge walk (Welcome Center / Crissy Field views)",
            also="Palace of Fine Arts + Chestnut Street (Marina)",
            lunch="The Warming Hut café or Greens Restaurant (Fort Mason)",
            dinner="Gary Danko (book ahead) or street eats at Off the Grid Fort Mason",
            route="Muni bus 28/30 toward Golden Gate Bridge Welcome Center. Walk or rideshare to Palace of Fine Arts / Marina",
        ),
        DayIdea(
            title="Alcatraz & Fisherman's Wharf",
            go="Alcatraz Island ferry (book timed tickets ahead)",
            also="Fisherman's Wharf + Pier 39 sea lions",
            lunch="Boudin Bakery clam chowder bread bowl (Wharf)",
            dinner="Sotto Mare or Scoma's for seafood",
            route="Muni F-Line or walk to Pier 33 for Alcatraz. Pier 39 is a short waterfront walk from the ferry return",
        ),
        DayIdea(
            title="Mission & downtown icons",
            go="Painted Ladies + Alamo Square, then Mission District murals (Balmy Alley)",
            also="Union Square shopping or Cable Car turnaround at Powell & Market",
            lunch="La Taqueria or Tartine Manufactory (Mission)",
            dinner="State Bird Provisions or Liholiho Yacht Club (book ahead)",
            route="Muni Metro to Civic Center / walk to Alamo Square. BART 16th St Mission for murals. Cable car or Muni back downtown",
        ),
        DayIdea(
            title="Departure",
            go="Last stop: Chinatown Gate + Grant Avenue souvenirs OR Blue Bottle coffee",
            also="Hotel checkout → San Francisco International (SFO)",
            lunch="Good Mong Kok Bakery dim sum or airport meal at SFO",
            dinner="Light snack airside only",
            route="BART to SFO (AirTrain to terminals) or shared van / rideshare. Leave 3+ hrs before flight",
        ),
    ],
    "los angeles": [
        DayIdea(
            title="Arrival · Hollywood / Mid-Wilshire",
            go="Griffith Observatory + Hollywood Sign viewpoint",
            also="Hollywood Walk of Fame / TCL Chinese Theatre",
            lunch="In-N-Out Burger or Howlin' Ray's (if queue is short)",
            dinner="Republique or Night + Market Song",
            route="Fly/drive into LA; Metro B Line to Hollywood/Vine or rideshare to Griffith Observatory",
        ),
        DayIdea(
            title="Santa Monica & Venice",
            go="Santa Monica Pier + Third Street Promenade",
            also="Venice Beach boardwalk + Abbot Kinney",
            lunch="Bay Cities Italian Deli or Sidecar Doughnuts",
            dinner="Gjelina (Venice) or The Misfit (Santa Monica)",
            route="Metro E Line to Santa Monica; walk/bike path or rideshare to Venice",
        ),
        DayIdea(
            title="Arts & Downtown",
            go="The Getty Center (book timed entry) or LACMA",
            also="Grand Central Market + DTLA Arts District stroll",
            lunch="Guerilla Tacos or Eggslut (Grand Central Market)",
            dinner="Bestia or Bavel (Arts District — book ahead)",
            route="Metro to Downtown; rideshare/bus to Getty (parking or shuttle from station)",
        ),
        DayIdea(
            title="Departure",
            go="Last stop: The Grove / Farmers Market souvenirs OR beach sunrise",
            also="Hotel checkout → Los Angeles (LAX)",
            lunch="Airport meal at LAX or nearby Culver City bite",
            dinner="Light snack airside only",
            route="Metro + LAX FlyAway bus, or rideshare to LAX. Leave 3+ hrs before flight",
        ),
    ],
    "san diego": [
        DayIdea(
            title="Arrival · Gaslamp / Waterfront",
            go="Balboa Park museums + botanical gardens",
            also="San Diego Zoo (book timed entry) if energy allows",
            lunch="Prado at Balboa Park or Extraordinary Desserts",
            dinner="Cowboy Star or Born & Raised (Gaslamp)",
            route="Amtrak/Pacific Surfliner or drive from LA; trolley / rideshare to Balboa Park",
        ),
        DayIdea(
            title="Coast & La Jolla",
            go="La Jolla Cove seals + coastal walk",
            also="Birch Aquarium or Torrey Pines views",
            lunch="George's at the Cove or Puesto La Jolla",
            dinner="Ironside Fish & Oyster or Juniper & Ivy",
            route="Drive or bus to La Jolla; return to Gaslamp / Little Italy for dinner",
        ),
        DayIdea(
            title="Departure",
            go="Last stop: Little Italy farmers market / coffee",
            also="Hotel checkout → San Diego (SAN) or continue north to LAX",
            lunch="Morning Glory or James Coffee Co.",
            dinner="Light snack before departure",
            route="Trolley / rideshare to SAN, or Amtrak back toward LAX. Leave 3+ hrs before flight",
        ),
    ],
}

_ALIASES: dict[str, str] = {
    "tyo": "tokyo",
    "tokyo japan": "tokyo",
    "osaka japan": "osaka",
    "osa": "osaka",
    "sel": "seoul",
    "seoul korea": "seoul",
    "south korea": "seoul",
    "par": "paris",
    "paris france": "paris",
    "bkk": "bangkok",
    "bangkok thailand": "bangkok",
    "sin": "singapore",
    "singapore city": "singapore",
    "tpe": "taipei",
    "taipei taiwan": "taipei",
    "hkg": "hong kong",
    "hk": "hong kong",
    "lon": "london",
    "london uk": "london",
    "london england": "london",
    "united kingdom": "london",
    "sfo": "san francisco",
    "san francisco": "san francisco",
    "sf": "san francisco",
    "lax": "los angeles",
    "los angeles": "los angeles",
    "la": "los angeles",
    "san diego": "san diego",
    "san": "san diego",
}


def normalize_guide_city(destination: str) -> str:
    key = (destination or "").strip().lower()
    key = key.replace(",", " ").replace("  ", " ").strip()
    if key in _GUIDES:
        return key
    if key in _ALIASES:
        return _ALIASES[key]
    for name in _GUIDES:
        if name in key or key in name:
            return name
    return ""


def day_ideas_from_llm_plan(
    plan: list[dict[str, str]],
    *,
    nights: int,
) -> list[DayIdea]:
    """Convert LLM-arranged attraction route into DayIdea cards."""
    n = max(1, int(nights or 1))
    ideas: list[DayIdea] = []
    for row in plan[:n]:
        ideas.append(
            DayIdea(
                title=str(row.get("title") or "").strip() or "Sightseeing",
                go=str(row.get("go") or "").strip() or "Explore the city",
                also=str(row.get("also") or "").strip() or str(row.get("go") or ""),
                lunch=str(row.get("lunch") or "").strip() or "Lunch nearby",
                dinner=str(row.get("dinner") or "").strip() or "Dinner nearby",
                route=str(row.get("route") or "").strip() or "Local transit",
            )
        )
    while len(ideas) < n and ideas:
        ideas.append(ideas[min(1, len(ideas) - 1)])
    return ideas[:n] if ideas else []


def day_ideas_from_attractions(
    attractions: list[str],
    *,
    city: str,
    nights: int,
    meals: list[str] | None = None,
) -> list[DayIdea]:
    """Build day cards from Trip.com / scraped attraction names."""
    n = max(1, int(nights or 1))
    city_label = (city or "the city").strip() or "the city"
    names = [a.strip() for a in attractions if (a or "").strip()]
    # Drop noisy product lines
    cleaned: list[str] = []
    seen: set[str] = set()
    skip = re.compile(
        r"(?i)\b(eSIM|SIM card|wifi|wi-fi|airport express|lounge|transfer|private car|"
        r"charter|ticket only|voucher|buffet deal|JR Pass)\b"
    )
    for name in names:
        key = name.lower()
        if skip.search(name) or len(name) < 3 or len(name) > 90:
            continue
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(name)
    names = cleaned

    meal_pool = [
        m
        for m in (meals or [])
        if m and len(m) < 80 and not skip.search(m)
    ]
    if not meal_pool:
        meal_pool = [
            f"Casual lunch near the attraction in {city_label}",
            f"Popular local dinner spot in {city_label}",
            f"Cafe or bakery lunch in {city_label}",
            f"Neighborhood restaurant dinner in {city_label}",
        ]

    if not names:
        return _generic_ideas(city_label, "First-time")[: max(n, 1)]

    ideas: list[DayIdea] = []
    # Arrival
    a0 = names[0]
    a1 = names[1] if len(names) > 1 else names[0]
    ideas.append(
        DayIdea(
            title=f"Arrival · {city_label}",
            go=f"Hotel check-in, then {a0}",
            also=a1 if a1 != a0 else f"Evening walk near {a0}",
            lunch=meal_pool[0],
            dinner=meal_pool[1 % len(meal_pool)],
            route=f"Airport train / express bus to hotel, then local transit to {a0}",
        )
    )
    # Middle days — two attractions each
    mid_needed = max(0, n - 2)
    idx = 2 if len(names) > 2 else 0
    for d in range(mid_needed):
        go = names[idx % len(names)]
        idx += 1
        also = names[idx % len(names)]
        idx += 1
        if also == go and len(names) > 1:
            also = names[(idx) % len(names)]
        ideas.append(
            DayIdea(
                title=f"{city_label} · {go.split('(')[0].strip()[:28]}",
                go=go,
                also=also,
                lunch=meal_pool[(d * 2) % len(meal_pool)],
                dinner=meal_pool[(d * 2 + 1) % len(meal_pool)],
                route=f"Metro / bus / rideshare between {go.split('(')[0].strip()} and nearby stops",
            )
        )
    # Departure
    last = names[-1]
    ideas.append(
        DayIdea(
            title="Departure",
            go=f"Last stop: {last}" if n > 1 else a0,
            also=f"Hotel checkout → airport ({city_label})",
            lunch=meal_pool[0],
            dinner="Light snack only before the flight",
            route="Airport express / pre-booked transfer; leave 3+ hours before departure",
        )
    )
    # Trim/pad to exactly n days
    if len(ideas) > n:
        if n == 1:
            ideas = [ideas[0]]
        else:
            ideas = [ideas[0]] + ideas[1 : n - 1] + [ideas[-1]]
    while len(ideas) < n:
        ideas.insert(-1 if len(ideas) > 1 else len(ideas), ideas[min(1, len(ideas) - 1)])
    return ideas[:n]


def _pick_idea_for_city_day(
    city_key: str,
    *,
    day_index: int,
    days_in_city: int,
    city_day_i: int,
    styles: list[str] | None,
    is_trip_first: bool,
    is_trip_last: bool,
    transfer_note: str = "",
) -> DayIdea:
    """Pick arrival / mid / departure idea for one city stay day."""
    base = list(_GUIDES.get(city_key) or [])
    if not base:
        label = city_key.replace("_", " ").title()
        base = _generic_ideas(label, ((styles or ["First-time"])[0]))
    if days_in_city <= 1:
        idea = base[0]
    elif city_day_i == 0:
        idea = base[0]
    elif city_day_i == days_in_city - 1 and len(base) > 1:
        # Prefer a mid highlight over departure when this isn't the trip's last day
        if is_trip_last:
            idea = base[-1]
        else:
            mid = base[1:-1] if len(base) > 2 else base[1:]
            idea = mid[(city_day_i - 1) % len(mid)] if mid else base[0]
    else:
        mid = base[1:-1] if len(base) > 2 else base
        idea = mid[(city_day_i - 1) % len(mid)]

    title = idea.title
    also = idea.also
    route = idea.route
    if transfer_note and city_day_i == days_in_city - 1 and not is_trip_last:
        also = f"{idea.also}. Then {transfer_note}"
        if "Transfer" not in title:
            title = f"{title} · transfer day"
    if is_trip_first and city_day_i == 0 and "Arrival" not in title:
        title = f"Arrival · {title}"
    if is_trip_last and city_day_i == days_in_city - 1:
        title = idea.title if "Departure" in idea.title else f"Departure · {title}"
        # Use city's departure idea when available
        if len(base) > 1 and "Departure" in base[-1].title:
            idea = base[-1]
            title = idea.title
            also = idea.also
            route = idea.route
    return DayIdea(
        title=title,
        go=idea.go,
        also=also,
        lunch=idea.lunch,
        dinner=idea.dinner,
        route=route,
    )


def day_ideas_for_regional(
    route: object,
    nights: int,
    *,
    styles: list[str] | None = None,
) -> list[DayIdea]:
    """Build a multi-city day plan from a RegionalRoute."""
    from travel_agent.regions import RegionalRoute, regional_day_city_sequence

    if not isinstance(route, RegionalRoute):
        return []
    n = max(1, int(nights or 1))
    seq = regional_day_city_sequence(route)[:n]
    while len(seq) < n:
        seq.append(seq[-1] if seq else "san francisco")

    # Count days per city in order
    ideas: list[DayIdea] = []
    i = 0
    stay_idx = 0
    while i < n:
        city = seq[i]
        # length of this city run
        run = 1
        while i + run < n and seq[i + run] == city:
            run += 1
        transfer = ""
        if stay_idx < len(route.stays) - 1:
            nxt = route.stays[stay_idx + 1]
            transfer = (
                f"transfer to {nxt.city} ({route.internal_note or 'flight / train / car'})"
            )
        for j in range(run):
            ideas.append(
                _pick_idea_for_city_day(
                    city,
                    day_index=i + j,
                    days_in_city=run,
                    city_day_i=j,
                    styles=styles,
                    is_trip_first=(i + j == 0),
                    is_trip_last=(i + j == n - 1),
                    transfer_note=transfer if j == run - 1 else "",
                )
            )
        i += run
        stay_idx += 1
    return ideas[:n]


def day_ideas_for(
    destination: str,
    nights: int,
    *,
    styles: list[str] | None = None,
    attractions: list[str] | None = None,
    attraction_plan: list[dict[str, str]] | None = None,
    regional_route: object | None = None,
) -> list[DayIdea]:
    """Return one DayIdea per trip day, cycling curated or scraped content."""
    n = max(1, int(nights or 1))
    style = ((styles or ["First-time"])[0] if styles else "First-time").strip()
    dest_label = (destination or "the city").strip() or "the city"

    # LLM-arranged route from scraped Trip.com attractions (preferred when present)
    if attraction_plan:
        llm_ideas = day_ideas_from_llm_plan(attraction_plan, nights=n)
        if llm_ideas:
            return llm_ideas

    # Multi-city region (e.g. California → SF + LA)
    if regional_route is None:
        try:
            from travel_agent.regions import build_regional_route

            regional_route = build_regional_route(destination, n)
        except Exception:
            regional_route = None
    if regional_route is not None:
        regional = day_ideas_for_regional(regional_route, n, styles=styles)
        if regional:
            return regional

    city = normalize_guide_city(destination)
    base = list(_GUIDES.get(city) or [])
    if city == "san francisco":
        dest_label = "San Francisco"
    elif city == "los angeles":
        dest_label = "Los Angeles"

    if not base and attractions:
        base = day_ideas_from_attractions(
            list(attractions),
            city=dest_label,
            nights=n,
        )
    if not base:
        base = _generic_ideas(dest_label, style)

    ideas: list[DayIdea] = []
    for i in range(n):
        if n == 1:
            ideas.append(base[0])
            break
        if i == 0:
            ideas.append(base[0])
        elif i == n - 1:
            ideas.append(base[-1] if len(base) > 1 else base[0])
        else:
            mid = base[1:-1] if len(base) > 2 else base
            ideas.append(mid[(i - 1) % len(mid)])
    return ideas


def _parse_hhmm(raw: str) -> tuple[int, int] | None:
    m = re.match(r"^([01]?\d|2[0-3]):([0-5]\d)$", (raw or "").strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def _fmt_hhmm(h: int, m: int) -> str:
    h = max(0, min(23, h))
    m = max(0, min(59, m))
    return f"{h:02d}:{m:02d}"


def _add_minutes(h: int, m: int, delta: int) -> tuple[int, int]:
    total = h * 60 + m + delta
    total = max(0, min(23 * 60 + 59, total))
    return total // 60, total % 60


def _infer_transfer(prev: str, nxt: str, idea: DayIdea) -> tuple[str, int]:
    """Guess transport method + minutes between two timetable stops."""
    a = (prev or "").lower()
    b = (nxt or "").lower()
    route = (idea.route or "").lower()
    blob = f"{a} {b} {route}"

    # Airport / long transfers
    if any(k in a for k in ("land", "arrive", "airport", "nrt", "hnd", "icn", "cdg")) or any(
        k in b for k in ("airport", "flight departs", "transfer to airport")
    ):
        if "narita express" in route or "n'ex" in route or "nrt" in blob:
            return "Narita Express / airport train", 55
        if "keikyu" in route or "haneda" in blob or "hnd" in blob:
            return "Keikyu / Tokyo Monorail", 40
        if "arex" in route or "icn" in blob:
            return "AREX airport train", 50
        if "limousine" in route or "bus" in route:
            return "Airport limousine bus", 70
        if "elizabeth" in route or "heathrow express" in route or "lhr" in blob:
            return "Elizabeth line / Heathrow Express", 45
        if "gatwick" in route or "lgw" in blob:
            return "Gatwick Express / Thameslink", 50
        return "Airport transfer", 60

    # Same-neighborhood / meal near last stop
    if any(k in b for k in ("lunch", "dinner", "ramen", "sushi", "yakitori", "cafe", "bento", "gyoza")):
        if any(k in a for k in ("hotel", "check-in", "wake", "pack")):
            return "Walk", 8
        return "Walk", 10

    if "hotel" in b or "check-in" in b or "wake" in a:
        return "Walk", 8

    if "free time" in b or "coffee" in b:
        return "Walk", 8

    # City rail hints from the day's route string
    if "yamanote" in route:
        return "JR Yamanote Line", 12
    if "ginza line" in route or "tokyo metro" in route:
        return "Tokyo Metro", 18
    if "yurikamome" in route:
        return "Yurikamome", 20
    if "mtr" in route:
        return "MTR", 15
    if "elizabeth" in route or "tube" in route or "underground" in route:
        return "London Underground", 18
    if "bart" in route:
        return "BART", 25
    if "muni" in route or "cable car" in route:
        return "Muni / cable car", 20
    if "metro" in route or "mrt" in route:
        return "Metro", 15
    if "jr " in route or "train" in route:
        return "Train", 25
    if "walk" in route and "line" not in route:
        return "Walk", 15

    # Day-trip style
    if "day trip" in idea.title.lower() or any(k in blob for k in ("kamakura", "nara", "jiufen", "nikko")):
        if "return" in b:
            return "JR / limited express return", 60
        return "JR / limited express", 55

    return "Metro / local transit", 20


def _with_transfers(
    slots: list[tuple[str, str]],
    idea: DayIdea,
) -> str:
    """Insert ↓ transport · ~N min lines between place rows."""
    if not slots:
        return ""
    lines: list[str] = [f"{slots[0][0]}  {slots[0][1]}"]
    for i in range(1, len(slots)):
        method, mins = _infer_transfer(slots[i - 1][1], slots[i][1], idea)
        lines.append(f"↓ {method} · ~{mins} min")
        lines.append(f"{slots[i][0]}  {slots[i][1]}")
    return "\n".join(lines)


def format_day_body(
    idea: DayIdea,
    *,
    arrive_time: str = "",
    return_depart_time: str = "",
) -> str:
    """Timetable with place rows and ↓ transfer legs (method + ETA).

    Arrival day never schedules activities before the flight lands.
    """
    title_l = idea.title.lower()
    arrive = _parse_hhmm(arrive_time)
    ret_dep = _parse_hhmm(return_depart_time)

    if "arrival" in title_l:
        if arrive:
            land_h, land_m = arrive
            land_mins = land_h * 60 + land_m
            t0 = (land_h, land_m)
            t1 = _add_minutes(land_h, land_m, 75)  # immigration + transfer
            slots: list[tuple[str, str]] = [
                (_fmt_hhmm(*t0), "Land at airport"),
                (_fmt_hhmm(*t1), "Hotel check-in and drop bags"),
            ]
            nearby = (
                idea.also
                if "observatory" in idea.also.lower() or "night" in idea.also.lower()
                else idea.go
            )
            # Late evening arrival (≥18:00): short evening only — no daytime sightseeing
            if land_mins >= 18 * 60:
                dinner_at = _add_minutes(land_h, land_m, 105)
                if dinner_at[0] < 23 or (dinner_at[0] == 22 and dinner_at[1] <= 45):
                    meal = idea.dinner or "Easy dinner near the hotel"
                    if land_mins >= 21 * 60:
                        slots.append(
                            (
                                _fmt_hhmm(*_add_minutes(land_h, land_m, 90)),
                                "Rest at hotel after the flight",
                            )
                        )
                    else:
                        slots.append((_fmt_hhmm(*dinner_at), meal))
                else:
                    slots.append(
                        (
                            _fmt_hhmm(*_add_minutes(land_h, land_m, 90)),
                            "Rest at hotel after the flight",
                        )
                    )
            else:
                t2 = _add_minutes(land_h, land_m, 105)
                t3 = _add_minutes(land_h, land_m, 150)
                if t2[0] < 23 or (t2[0] == 22 and t2[1] <= 45):
                    if t2[0] >= 21:
                        slots.append(
                            (
                                _fmt_hhmm(*t2),
                                f"Light neighborhood walk near hotel ({nearby.split(',')[0]})",
                            )
                        )
                    else:
                        slots.append((_fmt_hhmm(*t2), nearby))
                # Morning/afternoon arrivals: include lunch before dinner
                if t2[0] < 15:
                    t_lunch = _add_minutes(land_h, land_m, 180)
                    t_also = _add_minutes(land_h, land_m, 270)
                    t_dinner = _add_minutes(land_h, land_m, 540)
                    if idea.lunch:
                        slots.append((_fmt_hhmm(*t_lunch), idea.lunch))
                    if idea.also and idea.also != nearby:
                        slots.append((_fmt_hhmm(*t_also), idea.also))
                    if t_dinner[0] < 22 or (t_dinner[0] == 21):
                        slots.append((_fmt_hhmm(*t_dinner), idea.dinner))
                elif t3[0] < 23 or (t3[0] == 22 and t3[1] <= 50):
                    slots.append((_fmt_hhmm(*t3), idea.dinner))
                if len(slots) < 3:
                    slots.append(
                        (
                            _fmt_hhmm(*_add_minutes(land_h, land_m, 90)),
                            "Rest at hotel after the flight",
                        )
                    )
        else:
            # No live arrival yet — evening-leaning stub (never morning sightseeing)
            slots = [
                ("18:00", "Land at airport (time from flight card)"),
                ("19:15", "Hotel check-in and drop bags"),
                ("20:00", "Easy dinner near the hotel" if not idea.dinner else idea.dinner),
                ("21:00", "Rest at hotel after the flight"),
            ]
    elif "departure" in title_l:
        if ret_dep:
            dep_h, dep_m = ret_dep
            leave_h, leave_m = _add_minutes(dep_h, dep_m, -210)
            leave_mins = leave_h * 60 + leave_m
            slots = []
            if leave_mins >= 8 * 60:
                slots.append((_fmt_hhmm(*_add_minutes(leave_h, leave_m, -150)), idea.go))
                slots.append((_fmt_hhmm(*_add_minutes(leave_h, leave_m, -75)), idea.lunch))
            elif leave_mins >= 6 * 60:
                slots.append((_fmt_hhmm(*_add_minutes(leave_h, leave_m, -60)), idea.go))
            else:
                wake_h, wake_m = _add_minutes(leave_h, leave_m, -40)
                if wake_h * 60 + wake_m < 4 * 60:
                    wake_h, wake_m = 4, 0
                if (wake_h, wake_m) >= (leave_h, leave_m):
                    wake_h, wake_m = _add_minutes(leave_h, leave_m, -30)
                slots.append((_fmt_hhmm(wake_h, wake_m), "Wake up, pack, and hotel checkout"))
            slots.append((_fmt_hhmm(leave_h, leave_m), "Transfer to airport"))
            slots.append((_fmt_hhmm(dep_h, dep_m), f"Flight departs at {_fmt_hhmm(dep_h, dep_m)}"))
            slots = sorted(slots, key=lambda x: x[0])
        else:
            slots = [
                ("09:00", idea.go),
                ("10:30", idea.lunch),
                ("12:00", idea.also),
                ("13:30", "Depart for airport"),
            ]
    elif "day trip" in title_l or any(
        k in title_l for k in ("nikko", "kamakura", "jiufen", "nara")
    ):
        slots = [
            ("08:00", "Depart city station"),
            ("10:00", idea.go),
            ("12:30", idea.lunch),
            ("14:00", idea.also),
            ("17:00", "Return to city station"),
            ("19:30", idea.dinner),
        ]
    else:
        # Place-first timetable (route used as transfer metadata, not a stop)
        slots = [
            ("09:30", idea.go),
            ("12:30", idea.lunch),
            ("14:00", idea.also),
            ("17:30", "Free time / coffee near last stop"),
            ("19:00", idea.dinner),
        ]
    return _with_transfers(slots, idea)


def format_day_title(idea: DayIdea, day_num: int) -> str:
    return f"Day {day_num} - {idea.title}"


def is_vague_day_body(body: str) -> bool:
    """True when a day card still has generic or fluffy wording."""
    low = (body or "").lower()
    markers = (
        "attractions & tours",
        "deeper first-time picks",
        "deeper food picks",
        "deeper culture picks",
        "flexible free time",
        "local dinner and unwind",
        "continue city highlights",
        "continue highlights",
        "easy dinner nearby",
        "neighborhood walk",
        "top highlights and neighborhoods",
        "secondary area or deeper",
        "first-time picks in",
        "historical sites like",
        "explore the",
        "visit historical",
        "panoramic views of the city",
        "top landmark / viewpoint",
        "regional specialty lunch",
        "well-reviewed local restaurant",
        "second highlight in the same district",
        "hotel check-in, then easy landmark",
        "sunset viewpoint recommended by hotel",
        "ask hotel for today's best area",
        "casual cafe near the hotel",
        "market or food-hall lunch",
        "night-market / bistro style dinner",
        "last souvenir stop near the station",
        "best-known landmark",
        "well-known local lunch spot",
        "riverfront / skyline viewpoint",
        "second district of",
        "shopping street or creative quarter",
        "food hall or market lunch in that district",
        "park, bridge, or observation deck",
    )
    if any(m in low for m in markers):
        return True
    has_meal = bool(
        re.search(r"\b(1[0-2]|13):[0-5]\d\b", body or "")
        and re.search(r"\b(18|19|20):[0-5]\d\b", body or "")
    )
    has_route = any(x in low for x in ("metro", "mtr", " train", "line:", " bus", "jr ", "mrt ", "tube", "underground"))
    has_timetable = bool(re.search(r"\b([01]?\d|2[0-3]):[0-5]\d\b", body or ""))
    return not (has_meal and has_route and has_timetable)


def should_use_destination_guide(
    destination: str,
    days: list,
    *,
    nights: int = 0,
) -> bool:
    """Use curated place lists when days are missing, too few, or mostly vague."""
    n = max(0, int(nights or 0))
    city = normalize_guide_city(destination)
    if not days:
        return True
    # Always fill out the full trip length (fixes "only 1 day" when the LLM
    # returns a single detailed day for a multi-night stay).
    if n and len(days) < n:
        return True
    if not city:
        return False
    vague_n = sum(1 for d in days if is_vague_day_body(getattr(d, "body", "") or ""))
    return vague_n >= max(1, (len(days) + 1) // 2)


def _generic_ideas(dest: str, style: str) -> list[DayIdea]:
    """Last-resort day ideas when no curated city guide exists.

    Still uses concrete-sounding named anchors where possible so the UI never
    shows empty template phrases like "Top landmark / viewpoint in …".
    """
    city = (dest or "the city").strip() or "the city"
    low = style.lower()
    if "food" in low:
        go = f"{city} central food hall or market (ask hotel for today's best hall)"
        also = f"Second food street / night market recommended for {city}"
        lunch = f"Signature local dish at a named stall the hotel recommends"
        dinner = f"Booked specialty restaurant in {city} (hotel concierge pick)"
    elif "culture" in low:
        go = f"{city} main museum or palace (book timed entry if required)"
        also = f"Old-town walking street or cathedral square in {city}"
        lunch = f"Cafe inside or beside the museum"
        dinner = f"Traditional restaurant in the historic centre of {city}"
    elif "family" in low:
        go = f"{city} zoo, aquarium, or large city park"
        also = f"Hands-on science museum or observation deck in {city}"
        lunch = f"Casual family restaurant near the park"
        dinner = f"Early dinner near the hotel in {city}"
    else:
        go = f"{city} best-known landmark (palace, tower, or old town square)"
        also = f"{city} riverfront / skyline viewpoint"
        lunch = f"Well-known local lunch spot beside the landmark"
        dinner = f"Popular evening restaurant district in {city}"

    return [
        DayIdea(
            title=f"Arrival · {city}",
            go=f"Hotel check-in, then {city}'s nearest major landmark",
            also=f"Easy evening walk or viewpoint close to the hotel",
            lunch=f"Cafe or bakery near the hotel",
            dinner=dinner,
            route=f"Airport train / express bus to the hotel, then walk or metro to nearby sights",
        ),
        DayIdea(
            title=f"Highlights · {city}",
            go=go,
            also=also,
            lunch=lunch,
            dinner=dinner,
            route=f"Metro / bus from hotel to the main landmark; walk between nearby stops",
        ),
        DayIdea(
            title=f"Neighborhoods · {city}",
            go=f"Second district of {city} (shopping street or creative quarter)",
            also=f"Park, bridge, or observation deck photo stop in {city}",
            lunch=f"Food hall or market lunch in that district",
            dinner=f"Neighborhood bistro near the hotel",
            route=f"Metro or tram between districts; keep rides under 30 minutes when possible",
        ),
        DayIdea(
            title="Departure",
            go=f"Last souvenir stop near the main station in {city}",
            also="Hotel checkout → airport / station",
            lunch="Simple meal at the station or airport",
            dinner="Light snack only before the flight",
            route="Airport express train or pre-booked taxi; leave 3+ hours before departure",
        ),
    ]
