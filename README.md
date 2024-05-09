# gtnhissuehelper

Helps to parse the issue and gives some automated response, either to help the issue owner or to help dev debug issues.

## Features

1. Guess world corruption
2. Suggest angelica removal
3. Detect truncated crash reports
4. Tell the user to upload fml-client-latest.log instead if being greeted by a NPE from FMLProxyPacket
5. Diff the reported modlist with the reported modpack version. NOTE: currently limited to V2 manifest. 
6. Detect dev jar.
7. Detect installing both angelica and optifine 

## Refactoring

This github action setup's code style lean towards a simple throwaway script rather than a full-fledged application.
Unless it becomes much more complicated than it is now, I have no intention to change this.

In any case, a refactor is always welcomed AS LONG AS it's accompanied by some new features. 

## Limitations

Cannot parse release manifest in v1. These are pretty old though.