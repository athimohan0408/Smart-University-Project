# Who sits where: the 8th floor vs the faculty sheet

For **rag-core** (Portion 1). Checked by Portion 3 from the team's phone photos of the 8th floor (taken
3 November 2026): the name plate on each cabin door, and the "Faculty Room" directory board at the C817 entrance.
Compared with `rag-core/data/processed/faculty.json` (built from `8th_floor_faculty_final_data.xlsx`).

The kiosk answers "where is Dr X?" from the faculty sheet, so **where the sheet and the door disagree, visitors are
sent to the wrong cabin** (e.g. "where is Dr Nisha Varghese?" -> room 874, whose door says Bhuvana Jayabalan).
Nothing here has been changed in rag-core: please confirm with the department and update the sheet, then
`just index`.

| room | door name plate (photo) | directory board | faculty sheet | |
|---|---|---|---|---|
| 871 | Shivangi Singh (WA0249, WA0264) | *(blank)* | Dr Neetha S S, Dr Shivangi Singh | partly matches |
| 872 | Cecil Donald A (WA0254, WA0260) | Cecil Donald A | Dr Binayak Dutta, Dr Syam Mohan E | **differs** |
| 873 | Helen K Joy (WA0272, WA0279) | Helen K Joy | *(not listed; Dr Helen K Joy is in 885)* | **differs** |
| 874 | Bhuvana Jayabalan (WA0231) | Bhuvana Jayabalan | Dr Nisha Varghese | **differs** |
| 875 | *(no photo)* | Sridevi R | Dr Sridevi R | matches |
| 876 | Cynthia T (WA0205-0207) | Cynthia T | Dr Cynthia T | matches |
| 877 | Peter Augustine D (WA0219, WA0220) | Peter Augustine D | Dr Deepa V Jose *(Dr Peter Augustin D: 974)* | **differs** |
| 878 | Rupali Sunil Wagh (WA0221-0223) | Rupali Sunil Wagh | Dr Gobi R *(Dr Rupali Sunil Wagh: 628-11)* | **differs** |
| 879 | Somnath Sinha (WA0226, WA0227) | Somnath Sinha | Dr Somnath Sinha, Dr Thirunavukkarasu V | matches (sheet has one more) |
| 880 | *(no photo)* | Gobi R | Dr Sudhakar T | **differs** |
| 881 | Jossy P George (WA0212-0214) | Fr. Jossy P George | Dr Tegil J John *(Dr Jossy P George: 014)* | **differs** |
| 882 | Nizar Banu P K (WA0201-0204) | Nizar Banu P K | *(not listed; Dr Nizar Banu P K: 607A)* | **differs** |
| 883 | Chandra J (WA0199, WA0200) | Chandra J | Dr Chandra J | matches |
| 884 | Kavitha R (WA0230) | Kavitha R | Dr Kavitha R | matches |
| 885 | Vaidhehi V (WA0277) | Vaidhehi V | Dr Neha Singhal, Dr Helen K Joy | **differs** |
| 886 | *(no photo)* | *(blank)* | *(not listed)* | |
| 887 | *(no photo)* | Ashok Immanuel V | *(not listed)* | **missing from sheet** |
| 888 | Rajesh Kanna R (WA0270) | Rajesh Kanna R | *(not listed; Dr Rajesh Kanna R: 322)* | **differs** |
| 889 | *(no photo)* | *(blank)* | *(not listed)* | |
| 890 | Vijay Arputharaj J (WA0245, WA0250) | *(blank)* | Dr Vijay Arputharaj J | matches |

**Summary:** 6 rooms match (875, 876, 879, 883, 884, 890), 871 partly; **11 differ** (872, 873, 874,
877, 878, 880, 881, 882, 885, 887, 888). Where the door plate and the board agree, they are very likely current.

People the sheet places on this floor who have no door here: Dr Nisha Varghese, Dr Deepa V Jose, Dr Binayak Dutta,
Dr Syam Mohan E, Dr Sudhakar T, Dr Tegil J John, Dr Neha Singhal, Dr Neetha S S, Dr Thirunavukkarasu V.
People with a door or board entry here who are missing from the sheet or listed elsewhere: Cecil Donald A,
Bhuvana Jayabalan, Vaidhehi V, Ashok Immanuel V, Peter Augustine D, Rupali Sunil Wagh, Jossy P George, Nizar
Banu P K, Rajesh Kanna R, Helen K Joy, Gobi R (board: 880).

The original photos are in the team's "8th floor photos" folder (file names `IMG-20261103-WA####.jpg`).
