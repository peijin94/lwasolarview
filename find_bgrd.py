input: datedir

make a function according to the following


        if os.path.isdir(datedir):
            os.chdir(datedir)
            files=glob.glob("*")
            files.sort()

            background_found=False
            for j,file1 in enumerate(files[:-1]):
                hms1=file1.split('_')[1]
                hms2=files[j+1].split('_')[1]

                hour1=int(hms1[0:2])
                minute1=int(hms1[2:4])
                hour2=int(hms2[0:2])
                minute2=int(hms2[2:4])

                if hour1>19 and hour1<22:
                    file_time1=dt.datetime(st.year,st.month,st.day,hour1,minute1,0)
                    file_time2=dt.datetime(st.year,st.month,st.day,hour2,minute2,0)

                    if (file_time2-file_time1).seconds<300: ### 5 minutes
                        background_file=file1
                        background_found=True
                        print (file1,files[j+1])
                        break



output file1
