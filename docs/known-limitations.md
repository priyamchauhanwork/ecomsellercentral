# Known limitations (Phase 0)

Share this list with the pilot seller before onboarding (#37, #39).

## Orders

- **"Mark shipped" and "Mark delivered" don't update Shopify.** They change the status in SellerCentral only (with a timeline entry); no fulfillment is created on Shopify, the customer gets no shipping email from Shopify, and Shopify still shows the order as unfulfilled. Fulfil the order in Shopify as well. (#18)
- **A Shopify update never undoes a manual status.** Shopify can only move an order forward (pending → shipped → delivered, or cancelled), so an order you marked shipped stays shipped even if Shopify still says unfulfilled. (#9)
- **Manual status changes only go forward:** pending → shipped / delivered / cancelled, shipped → delivered. A mistaken "shipped" can't be undone from the app. (#10)

## Products

- **A single SKU can't be hidden on Shopify.** Taking one variant off sale from the app shows it as *sold out* on the storefront (quantity 0, overselling off, stock tracking turned on) rather than removing it; putting it back on sale restores the app's stock figure. (#47)
- **Taking a product off sale makes it a draft on Shopify,** which hides it from every sales channel, not only the Online Store. (#47)
- **Product images are links, not uploads.** (#45)
- **Products can't be edited in the app yet;** edit them in Shopify and re-import, or create a new one. (#45)

## Inventory

- **One stock figure per SKU,** for one location (Shopify's primary location). Multi-warehouse stock is a later phase. (#19)
- **Cancelling in Shopify without restocking still restocks here.** The app gives back the units of a cancelled order (or of items removed by an order edit) even if "Restock items" was unticked in Shopify; the inventory page then shows that SKU as different from Shopify. Adjust it by hand. (#21)
- **Orders placed before your products were imported don't change stock,** because the imported stock already reflected them. Their lines say so on the order page. (#21)
- **The app's figure wins on Shopify.** Adjusting stock in the app sets Shopify's available quantity to the app's figure. Edit stock in the app, not in the Shopify admin, or the two will differ until you push the app's figure again from the inventory page. (#20)
- **A sale on Shopify in the seconds between its order and the app receiving it can be overwritten** by a manual adjustment made in exactly that window; the inventory page then shows the difference. (#20)

