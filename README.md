Vilniaus Universiteto

Matematikos ir Informatikos Fakulteto

Programų Sistemų studijos programos

4 kurso, 1 grupės studento

Gusto Akstino

Baigiamojo bakalaurinio darbo projektas

# Įrankio paleidimas

Įrankį sudaro 3 komponentai: 

1. laboratorijų valdiklis (lab host), atsakingas už Docker resursų valdymą
2. centirnis valdiklis (control plane), atsakingas už aukšesnio lygio veiksmų koordinavimą
3. komandinės eilutės įrankiai (cli), suteikiantys administratoriui prieigą prie centrinio valdiklio

Visi komponentai yra konteinerizuoti, vadinasi gali būti naudojami be papildomo paruošimo and Ubuntu ir Debian platformų, jeigu suinstaliuotas docker.

Darbui su įrankiu pradėti užtenka sukurti Docker konteinerį su komandinės eilutės įrankiais, naudojant `docker run` komandą:

```
docker run --rm -it \
  --name cli-shell \
  -v "$(pwd)/cli/data:/app/cli/data" \
  -v /var/run/docker.sock:/var/run/docker.sock \
  --network host \
  ghcr.io/akselis/docker-interface-cli:latest
```

Ši komanda sukuria Docker konteinerį su pavadinimu `cli-shell`, kuriame yra įrankio komandinės eilutės įrankiai, ir prikabina prie terminalo. Galima pradėti darbą parašant komandą `--help`.

## Paprasčiausios laboratorijos parengimas

Norint paleisti virtualią laboratoriją naudojant komandinės eilutės įrankius, privaloma visų pirmiausia įdiegti centrinį bei laboratorijų valdiklius. Bet prieš tai, turime apibrėžti tinkamus hostus:

```
infra provision --type local
```

Su šia komanda, pridedame naudojamą kompiuterį, kuriame galima bus įdiegti valdiklius.

```
infra bootstrap control-plane --device localhost --api-key raktas
```

Ši komanda pasirūpina, kad pasirinktas kompiuteris turi visas priklausomybes valdyti Docker ir įdiegia centrinį valdiklį. Po to galime įdiegti laboratorijų valdiklį su:

```
infra bootstrap lab-host --device localhost --api-key raktas --use-routing 
```

Ši komanda įdiegia laboratorijų valdiklį su automatiniu tinklų valdymu, kad paleistos paslaugos būtų lengvai pasiekiamos išoriškai. Po diegimo, `lab-host` yra užregistruojamas centriniame valdiklyje, kad būtų galima iš jo šiame įrenginyje valdyti Docker resursus. Dabar atsiveria galimybės diegti konteinerius, `docker compose` projektus ir t.t.

Komanda `cp --health` suteiks visą informaciją, kaip judėti toliau.
