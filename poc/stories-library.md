## H-01 Create a book
An editor wants to add a book so that it becomes part of the catalog.
Status: approved
Only: editor
Sessions:
- "reader-local": Lena (reader)
- "editor-local": Eva (editor)
- "admin-local": Ada (administrator)
Things:
- book (table): title (text, required)
- member (table): name (text, required)
- loan (table): book (number, required, references book), member (number, required, references member)
Cases:
- C-01 [happy] As: "editor-local" Given nothing  When POST /books {"title": "Dune"}  Then 201 {"id": 1, "title": "Dune"}  Creates: book
- C-02 [error] As: nobody Given nothing  When POST /books {"title": "Dune"}  Then 401 {"error": "unauthorized"}  Rule: BR-01
BR-01 A user must be signed in as an editor to create books. Error: unauthorized. Cases: C-02

## H-02 Create a member
An editor wants to add a member so that they can borrow books.
Status: approved
Only: editor
Sessions:
- "reader-local": Lena (reader)
- "editor-local": Eva (editor)
- "admin-local": Ada (administrator)
Cases:
- C-03 [happy] As: "admin-local" Given nothing  When POST /members {"name": "Luis"}  Then 201 {"id": 1, "name": "Luis"}  Creates: member
- C-04 [error] As: "reader-local" Given nothing  When POST /members {"name": "Luis"}  Then 403 {"error": "forbidden"}  Rule: BR-02
BR-02 A reader cannot create members. Error: forbidden. Cases: C-04

## H-03 Create a loan
An editor wants to lend a book to a member so that the handoff is recorded.
Status: approved
Only: editor
Sessions:
- "reader-local": Lena (reader)
- "editor-local": Eva (editor)
- "admin-local": Ada (administrator)
Cases:
- C-05 [happy] As: "editor-local" Given book 1 {"title": "Dune"}; member 2 {"name": "Luis"}  When POST /loans {"book": 1, "member": 2}  Then 201 {"id": 1, "book": 1, "member": 2}  Creates: loan
- C-06 [error] As: "editor-local" Given member 2 {"name": "Luis"}  When POST /loans {"book": 99, "member": 2}  Then 404 {"error": "not_found"}  Creates: loan  Rule: BR-03
- C-07 [error] As: nobody Given nothing  When POST /loans {"book": 1, "member": 2}  Then 401 {"error": "unauthorized"}  Creates: loan  Rule: BR-04
BR-03 A loan is rejected if its book or member does not exist. Error: not_found. Cases: C-06
BR-04 A user must be signed in as an editor to create loans. Error: unauthorized. Cases: C-07

## H-04 List books
A reader wants to list books so that they can browse the catalog.
Status: approved
Only: reader
Sessions:
- "reader-local": Lena (reader)
- "editor-local": Eva (editor)
- "admin-local": Ada (administrator)
Cases:
- C-08 [happy] As: "reader-local" Given book 1 {"title": "Dune"}  When GET /books  Then 200 {"items": [{"id": 1, "title": "Dune"}]}
- C-09 [error] As: nobody Given nothing  When GET /books  Then 401 {"error": "unauthorized"}  Rule: BR-05
BR-05 A user must sign in to list books. Error: unauthorized. Cases: C-09

## H-05 Read a book
A reader wants to view a book so that they can check its title.
Status: approved
Only: reader
Sessions:
- "reader-local": Lena (reader)
- "editor-local": Eva (editor)
- "admin-local": Ada (administrator)
Cases:
- C-10 [happy] As: "reader-local" Given book 1 {"title": "Dune"}  When GET /books/1  Then 200 {"id": 1, "title": "Dune"}
- C-11 [error] As: "reader-local" Given nothing  When GET /books/99  Then 404 {"error": "not_found"}  Rule: BR-06
- C-12 [error] As: nobody Given nothing  When GET /books/1  Then 401 {"error": "unauthorized"}  Rule: BR-07
BR-06 A missing book returns not found. Error: not_found. Cases: C-11
BR-07 A user must sign in to read a book. Error: unauthorized. Cases: C-12

## H-06 Delete a book
An administrator wants to delete a book so that it is removed from the catalog.
Status: approved
Only: administrator
Sessions:
- "reader-local": Lena (reader)
- "editor-local": Eva (editor)
- "admin-local": Ada (administrator)
Cases:
- C-13 [happy] As: "admin-local" Given book 1 {"title": "Dune"}  When DELETE /books/1  Then 204
- C-14 [error] As: "editor-local" Given book 1 {"title": "Dune"}  When DELETE /books/1  Then 403 {"error": "forbidden"}  Rule: BR-08
- C-15 [error] As: nobody Given book 1 {"title": "Dune"}  When DELETE /books/1  Then 401 {"error": "unauthorized"}  Rule: BR-09
BR-08 An editor cannot delete books. Error: forbidden. Cases: C-14
BR-09 An administrator must sign in to delete books. Error: unauthorized. Cases: C-15
