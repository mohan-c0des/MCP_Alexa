class Student:
    def __init__(self, name, grades=[]):
        self.name = name
        self.grades = grades

    def add_grade(self, grade):
        self.grades.append(grade)

    def average(self):
        return sum(self.grades) / len(self.grades)
def create_students():
    students = []
    names = ["Alice", "Bob", "Charlie"]
    for name in names:
        students.append(Student(name))
    return students
def populate_grades(students):
    students[0].add_grade(90)
    students[1].add_grade(70)
    students[2].add_grade(80)
def display_results(students):
    for student in students:
        print(f"{student.name}: {student.grades}")
        print(f"Average = {student.average()}\n")

students = create_students()
populate_grades(students)
display_results(students)