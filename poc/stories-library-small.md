## H-01 Add books
A librarian wants to add a book so that it is in the catalog.
Status: approved
Things:
- book (table): title (text, required, unique), pages (number)
Cases:
- C-01 [happy] Given nothing  When POST /books {"title": "Dune", "pages": 412}  Then 201 {"id": 1}  Creates: book
- C-02 [error] Given nothing  When POST /books {"pages": 412}  Then 422 {"error": "validation_error"}  Rule: BR-01
- C-03 [error] Given book 1 {"title": "Dune"}  When POST /books {"title": "Dune"}  Then 409 {"error": "conflict"}  Creates: book  Rule: BR-02

BR-01 A book title is required. Error: validation_error. Cases: C-02
BR-02 Two books cannot share the same title. Error: conflict. Cases: C-03

## H-02 Read a book
A librarian wants to read a book so that they can see its page count.
Status: approved
Cases:
- C-04 [happy] Given book 1 {"title": "Dune", "pages": 412}  When GET /books/1  Then 200 {"title": "Dune", "pages": 412}
- C-05 [error] Given nothing  When GET /books/99  Then 404 {"error": "not_found"}  Rule: BR-03

BR-03 A missing book returns not found. Error: not_found. Cases: C-05
